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

PROJECT = Path("/home/chavez_f/code/quant-tools")
SCRIPTS = PROJECT / "scripts"
PY = "/usr/bin/python3.12"
CONFIG = Path(os.environ.get("QUANT_CONFIG", PROJECT / "config.json"))
RUN_POINTER = PROJECT / "state" / "latest-planning-run.json"
TELEGRAM_LIMIT = 4000

# Make the toolkit's common.py helpers importable (derive_sizing_mode +
# parse_regime_from_brief). Cron is at ~/.hermes/scripts/ so the repo-relative
# path is the only one that works without a symlink or sys.path dance.
sys.path.insert(0, str(SCRIPTS))
try:
    from common import derive_sizing_mode, parse_regime_from_brief
except ImportError as e:
    print(f"⚠️ cron_executable_scan: cannot import common helpers ({e}); will default to cautious", file=sys.stderr)

    def parse_regime_from_brief(_brief_text: str) -> str | None:  # type: ignore[no-redef]
        return None

    def derive_sizing_mode(_brief_text: str) -> tuple[str, str | None]:  # type: ignore[no-redef]
        return "cautious", None


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M ET")


def find_morning_brief() -> str | None:
    """Read the exact brief recorded by today's successful planning run."""
    if not RUN_POINTER.exists():
        return None
    try:
        pointer = json.loads(RUN_POINTER.read_text())
        created = datetime.fromisoformat(str(pointer.get("created_at")))
        brief_path = Path(str(pointer.get("brief") or ""))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if created.date() != datetime.now().date() or not brief_path.exists():
        return None
    return brief_path.read_text()


def fetch_brief() -> str:
    """Run daily_brief.py WITHOUT --send so we get the text to embed."""
    print(f"[{now_str()}] fetching daily brief (fallback)", file=sys.stderr)
    try:
        proc = subprocess.run(
            [PY, "daily_brief.py", "--watchlist", "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD"],
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


def latest_report_dir(parent: Path) -> Path | None:
    """The slim pipeline writes into YYYYMMDD-HHMMSS/ subdirs; pick the newest."""
    if not parent.exists():
        return None
    subdirs = [d for d in parent.iterdir() if d.is_dir() and d.name[:8].isdigit()]
    if not subdirs:
        return None
    return sorted(subdirs, key=lambda d: d.name)[-1]


def format_management(report_dir: Path | None) -> str:
    """Pull close/roll signals from the management.json the slim pipeline wrote."""
    if not report_dir:
        return ""
    mgmt_path = report_dir / "management.json"
    if not mgmt_path.exists():
        return ""
    try:
        data = json.loads(mgmt_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  (management parse failed: {e})", file=sys.stderr)
        return ""
    summary = data.get("summary", {})
    open_n = summary.get("open_trades", 0)
    if open_n == 0:
        return ""
    lines = [f"\n🔁 OPEN POSITIONS ({open_n}): {summary.get('close', 0)} close · {summary.get('roll_or_close', 0)} roll · {summary.get('review', 0)} review · {summary.get('hold', 0)} hold"]
    actions = data.get("actions", [])[:5]
    for a in actions:
        dte = a.get("dte")
        dte_s = f"{dte}d" if dte is not None else "—"
        pct = a.get("unrealized_pnl_pct")
        pct_s = f"{pct:+.0f}%" if pct is not None else "—"
        reason = (a.get("reasons") or [""])[0]
        lines.append(
            f"  • {a.get('ticker', '?'):5s} {a.get('strategy', '?'):12s} "
            f"DTE={dte_s:>4s} P&L={pct_s:>6s} → {a.get('action', '?'):14s} {reason}"
        )
    return "\n".join(lines)


def format_top_tickets(report_dir: Path | None, n: int = 3) -> str:
    """Build the executable tickets section for the Telegram digest.

    Shows both:
      - APPROVE/STRONG candidates (executable as-is — the "ready to click" list)
      - REDUCE candidates with their reason (size it smaller because of the
        attribution THROTTLE / adaptive sizing, etc.)

    When nothing is actionable, the user can see whether (a) no candidates
    met the scoring bar at all, or (b) candidates met the bar but were
    downgraded. The second case is useful — the user might want to
    override the conservative default and execute at smaller size.
    """
    if not report_dir:
        return ""
    tickets_path = report_dir / "tickets.json"
    if not tickets_path.exists():
        return ""
    try:
        data = json.loads(tickets_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  (tickets parse failed: {e})", file=sys.stderr)
        return ""

    all_tickets = data.get("tickets", [])
    if not all_tickets:
        return "\n🎫 NO CANDIDATES TODAY (scan returned zero setups above the score floor)"

    actionable = [t for t in all_tickets if str(t.get("decision", "")).upper() in {"APPROVE", "STRONG"}]
    reduced = [t for t in all_tickets if str(t.get("decision", "")).upper() == "REDUCE"]

    out = [f"\n🎫 {len(all_tickets)} CANDIDATE{'S' if len(all_tickets) != 1 else ''}"]
    if actionable:
        top = actionable[:n]
        out.append(f"  ✅ EXECUTABLE ({len(actionable)} approved):")
        for t in top:
            out.append(
                f"    • {t.get('ticker', '?'):5s} {t.get('strategy', '?'):12s} "
                f"{t.get('expiration', '?'):>10s} {t.get('strikes', '?'):>10s} "
                f"credit≥{t.get('limit_credit', '?')} floor={t.get('do_not_chase_below', '?')} "
                f"score={t.get('score', '?')}"
            )
    if reduced:
        top = reduced[:n]
        out.append(f"  ⚠️  REDUCED SIZE ({len(reduced)} flagged):")
        for t in top:
            # The ticket's rationale dict carries the downgrader notes from
            # the action plan (adaptive_sizing, profile, correlation, etc).
            # Join any non-empty ones so the user sees *why* size was cut.
            rationale = t.get("rationale") or {}
            reasons = [
                str(v) for v in (
                    rationale.get("adaptive_sizing"),
                    rationale.get("profile"),
                    rationale.get("correlation"),
                ) if v
            ]
            reason_s = " · ".join(reasons)[:80] or "see plan for details"
            sm = t.get("size_multiplier")
            out.append(
                f"    • {t.get('ticker', '?'):5s} {t.get('strategy', '?'):12s} "
                f"{t.get('expiration', '?'):>10s} {t.get('strikes', '?'):>10s} "
                f"size×{sm if sm is not None else '?'} score={t.get('score', '?')} — {reason_s}"
            )
    if not actionable and not reduced:
        out.append(f"  (all {len(all_tickets)} HOLD — see plan for details)")
    return "\n".join(out)


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

    # 3. Compose message
    header = f"📊 10:30 EXECUTABLE SCAN — {now_str()}\nRegime: {verdict or 'UNKNOWN'} · sizing: {sizing_mode}"
    mgmt_text = format_management(latest)
    tickets_text = format_top_tickets(latest, n=3)

    composed = (header + mgmt_text + tickets_text).strip()
    if not composed:
        composed = f"📊 10:30 EXECUTABLE SCAN — {now_str()}\n(no content produced)"
    if len(composed) > TELEGRAM_LIMIT:
        composed = composed[: TELEGRAM_LIMIT - 60] + "\n... (truncated, full report at " + str(latest) + ")"

    # 4. Send
    if not send_telegram(composed):
        print("⚠️ Telegram send failed", file=sys.stderr)
        return 1

    print(f"✅ Executable scan delivered {now_str()} ({len(composed)} chars); reports at {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
