#!/usr/bin/env python3.12
"""
cron_executable_scan.py — No-agent cron entry for the 10:30 AM executable scan.

The 8:30 cron_morning_workflow runs the planning profile and records its exact
report directory in a run pointer. The 10:30 run is an executable refresh with
the chains now stable since
the open: re-mark open positions, re-run management, re-scan, re-plan,
re-allocate, re-build tickets, and send a compact "READY TO TRADE" Telegram
message with the top actionable strikes.

Differences from cron_morning_workflow:
  - Skips discovery (already done at 8:30)
  - Skips scenario-stress (sizing unchanged since morning)
  - Skips brief (chains are stable now, no need to re-print the macro
    digest — the 8:30 message already covered that)
  - Skips the dashboard HTML (Telegram-only)
  - KEEPS mark + management + scan + risk + plan + allocation + alerts +
    tickets + storage (the execution-heavy steps)

Schedule: 30 10 * * 1-5 (10:30 AM ET, weekdays). Paired with the 8:30
morning-market-brief cron; the 8:30 is the planning signal, the 10:30
is the execution prep.

Stdout = a status line. Heavy content goes through `hermes send` so we
stay under the 4096-char Telegram limit. Empty stdout = silent. Non-zero
exit = error alert via the scheduler.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT = Path(os.environ.get("QUANT_TOOLS_HOME", "/home/chavez_f/code/quant-tools"))
SCRIPTS = PROJECT / "scripts"
PY = os.environ.get("QUANT_PYTHON", "/usr/bin/python3.12")
CONFIG = Path(os.environ.get("QUANT_CONFIG", PROJECT / "config.json"))
RUN_POINTER = PROJECT / "state" / "latest-planning-run.json"

sys.path.insert(0, str(SCRIPTS))
from common import atomic_write_json, derive_sizing_mode
from hermes_ops import (
    DEFAULT_WATCHLIST,
    compose_executable_message,
    latest_report_dir,
    planning_brief,
    read_report,
)


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M ET")


def find_morning_brief() -> str | None:
    """Read the exact brief recorded by today's successful planning run."""
    return planning_brief(RUN_POINTER)


def fetch_brief() -> str:
    """Run daily_brief.py WITHOUT --send so we get the text to embed."""
    print(f"[{now_str()}] fetching daily brief (fallback)", file=sys.stderr)
    try:
        proc = subprocess.run(
            [PY, "daily_brief.py", "--watchlist", *DEFAULT_WATCHLIST],
            capture_output=True, text=True, timeout=180,
            cwd=str(SCRIPTS),
        )
    except subprocess.TimeoutExpired:
        return "(brief fetch timed out)"
    if proc.returncode != 0:
        return f"(brief fetch failed: {proc.stderr.strip()[:200]})"
    return proc.stdout.strip()


def run_executable_pipeline(report_dir: Path, sizing_mode: str) -> tuple[bool, str]:
    """Run the slim operator pipeline. Returns (ok, stderr_tail).

    The executable profile owns the supported slim-step defaults. The Telegram
    digest is composed here, not by the pipeline.
    """
    print(f"[{now_str()}] running slim operator (sizing-mode={sizing_mode}) -> {report_dir}", file=sys.stderr)
    try:
        proc = subprocess.run(
            [
                PY, "quant.py",
                "--config", str(CONFIG),
                "daily",
                "--profile", "executable",
                "--report-dir", str(report_dir),
                "--sizing-mode", sizing_mode,
            ],
            capture_output=True, text=True, timeout=600,
            cwd=str(SCRIPTS),
        )
    except subprocess.TimeoutExpired:
        return False, "pipeline timeout (>600s)"
    except Exception as e:
        return False, f"pipeline exception: {e}"

    if proc.returncode != 0:
        return False, proc.stderr.strip()[:300]
    return True, ""

def fetch_iv_ranks_for_tickets(
    tickets_report: dict[str, Any],
    *,
    timeout: int = 120,
) -> dict[str, float]:
    """Fetch live IVR for the actionable tickers in a tickets report.

    Returns a {ticker_upper: iv_rank} dict for the IVR-gate in
    compose_executable_message. Returns {} on any failure so the gate
    is bypassed (backward compat — see format_executable_tickets).

    Only APPROVE/STRONG tickets are queried, to minimize API calls.
    Duplicate tickers are deduped. The iv_rank.py subprocess is run from
    SCRIPTS so the existing import path resolves.
    """
    if not tickets_report:
        return {}
    seen: set[str] = set()
    for ticket in tickets_report.get("tickets", []):
        if str(ticket.get("decision", "")).upper() not in {"APPROVE", "STRONG"}:
            continue
        ticker = str(ticket.get("ticker", "")).strip().upper()
        if ticker:
            seen.add(ticker)
    if not seen:
        return {}
    try:
        proc = subprocess.run(
            [PY, "iv_rank.py", "--tickers", *sorted(seen), "--json"],
            capture_output=True, text=True, timeout=timeout,
            cwd=str(SCRIPTS),
        )
    except subprocess.TimeoutExpired:
        print(f"[{now_str()}] IVR fetch timed out after {timeout}s; gate bypassed", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"[{now_str()}] IVR fetch failed: {e}; gate bypassed", file=sys.stderr)
        return {}
    if proc.returncode != 0:
        print(
            f"[{now_str()}] iv_rank.py exited {proc.returncode}: {proc.stderr.strip()[:200]}; gate bypassed",
            file=sys.stderr,
        )
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[{now_str()}] iv_rank.py output not JSON: {e}; gate bypassed", file=sys.stderr)
        return {}
    out: dict[str, float] = {}
    for ticker, metrics in (payload.get("tickers") or {}).items():
        rank = metrics.get("iv_rank")
        if rank is None:
            continue
        try:
            out[str(ticker).upper()] = float(rank)
        except (TypeError, ValueError):
            continue
    return out


def send_telegram(message: str) -> bool:
    if not message.strip():
        return False
    if os.environ.get("EXECUTABLE_DRY_RUN"):
        print("---DRY-RUN MESSAGE START---")
        print(message)
        print("---DRY-RUN MESSAGE END---")
        return True
    result = subprocess.run(
        ["hermes", "send", "--to", "telegram", message],
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0


def send_failure_alert(stage: str, detail: str) -> None:
    msg = f"⚠️ Executable scan FAILED at {stage}\n{detail[:300]}"
    try:
        send_telegram(msg)
    except Exception:
        pass


def main() -> int:
    if not CONFIG.exists() or CONFIG.name == "config.example.json":
        print(f"cron_executable_scan requires a production config.json; got {CONFIG}", file=sys.stderr)
        return 1
    today = datetime.now().strftime("%Y-%m-%d")
    # Use a dedicated parent so the executable run doesn't get mistaken for
    # the 8:30 morning run when you browse reports/. The slim pipeline still
    # creates a YYYYMMDD-HHMMSS/ subdir under it.
    report_dir = PROJECT / "reports" / f"exec-{today}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # 1. Brief — prefer the 8:30 (already fetched, no extra API call), fall
    #    back to a fresh fetch if the 8:30 didn't produce one
    brief = find_morning_brief()
    if brief:
        print(f"[{now_str()}] using 8:30 brief from disk", file=sys.stderr)
    else:
        brief = fetch_brief()
    sizing_mode, verdict = derive_sizing_mode(brief)
    if verdict:
        print(f"[{now_str()}] macro regime={verdict} → sizing-mode={sizing_mode}", file=sys.stderr)
    else:
        print(f"[{now_str()}] could not parse regime from brief → sizing-mode={sizing_mode} (default)", file=sys.stderr)

    # 2. Slim pipeline
    ok, err = run_executable_pipeline(report_dir, sizing_mode)
    if not ok:
        send_failure_alert("pipeline", err)
        print(f"⚠️ Executable scan failed at pipeline: {err}", file=sys.stderr)
        return 1

    latest = latest_report_dir(report_dir)
    if not latest:
        # Mirror cron_morning_workflow.py: failing loud beats a silent empty
        # Telegram message. The pipeline subprocess reported success but
        # produced no timestamped subdir under report_dir.
        send_failure_alert(
            "manifest",
            f"executable pipeline reported success but no timestamped subdir under {report_dir}",
        )
        print(
            f"⚠️ Executable scan failed at manifest: no subdir under {report_dir}",
            file=sys.stderr,
        )
        return 1

    tickets_report = read_report(latest / "tickets.json") if latest else {}
    management_report = read_report(latest / "management.json") if latest else {}

    # 2b. Fetch live IVR for actionable tickers so the IVR gate in
    # compose_executable_message (format_executable_tickets) can demote
    # IVR<50 candidates from EXECUTABLE to HELD BY IVR. SCAN_DISCREPANCIES
    # item #5. Failure to fetch is non-fatal — an empty iv_ranks dict
    # bypasses the gate (backward compat).
    iv_ranks = fetch_iv_ranks_for_tickets(tickets_report)
    if iv_ranks:
        held = [
            t for t in tickets_report.get("tickets", [])
            if str(t.get("decision", "")).upper() in {"APPROVE", "STRONG"}
            and float(iv_ranks.get(str(t.get("ticker", "")).upper(), 100)) < 50
        ]
        print(
            f"[{now_str()}] IVR gate: {len(iv_ranks)} tickers checked, "
            f"{len(held)} held by IVR (<50)",
            file=sys.stderr,
        )

    # 2c. Persist the fetched IVR readings (plus the regime/sizing context
    # this message was composed under) next to tickets.json so offline
    # consumers — dashboard.py in particular — can reproduce the exact
    # gate applied here. Non-fatal: the dashboard treats a missing or
    # empty file as "gate not evaluated", the same semantics as
    # iv_ranks={} in compose_executable_message.
    if latest:
        try:
            atomic_write_json(
                latest / "iv_ranks.json",
                {
                    "as_of": datetime.now().isoformat(),
                    "source": "cron_executable_scan",
                    "regime": verdict,
                    "sizing_mode": sizing_mode,
                    "iv_ranks": iv_ranks,
                },
            )
        except OSError as e:
            print(f"[{now_str()}] could not persist iv_ranks.json: {e}", file=sys.stderr)

    # 3. Compose message
    composed = compose_executable_message(
        timestamp=now_str(),
        regime=verdict,
        sizing_mode=sizing_mode,
        management=management_report,
        tickets=tickets_report,
        report_dir=latest,
        iv_ranks=iv_ranks,
    )

    # 4. Send
    if not send_telegram(composed):
        print("⚠️ Telegram send failed", file=sys.stderr)
        return 1

    print(f"✅ Executable scan delivered {now_str()} ({len(composed)} chars); reports at {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
