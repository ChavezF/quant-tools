#!/usr/bin/env python3.12
"""
cron_morning_workflow.py — No-agent cron entry for the morning workflow.

Runs the 08:30 planning profile, then sends the brief plus planning alerts.
Candidates remain report artifacts and no execution tickets are persisted.

Stdout of this script = a status line; the heavy content goes through
`hermes send` explicitly so we can compose multiple report pieces within
the 4096-char Telegram limit.

This replaces the older cron_brief.py (which only ran daily_brief.py).
The schedule, workdir, deliver, and no-agent mode are unchanged on the
cron job — only the script field was swapped.

Empty stdout = silent (no cron error). Non-zero exit = error alert via
the scheduler.
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
TELEGRAM_LIMIT = 4000  # leave headroom under the 4096 hard limit

# Make the toolkit's common.py helpers importable (derive_sizing_mode +
# parse_regime_from_brief). Cron is at ~/.hermes/scripts/ so the repo-relative
# path is the only one that works without a symlink or sys.path dance.
sys.path.insert(0, str(SCRIPTS))
try:
    from common import derive_sizing_mode, parse_regime_from_brief, REGIME_TO_SIZING
except ImportError as e:
    print(f"⚠️ cron_morning_workflow: cannot import common helpers ({e}); will default to cautious", file=sys.stderr)
    # Stubs so the script can still run if common.py is unreachable for any
    # reason — sizing_mode falls through to cautious (the safer default).
    REGIME_TO_SIZING = {}

    def parse_regime_from_brief(_brief_text: str) -> str | None:  # type: ignore[no-redef]
        return None

    def derive_sizing_mode(_brief_text: str) -> tuple[str, str | None]:  # type: ignore[no-redef]
        return "cautious", None

# Reusable default watchlist for the brief; matches the morning brief script's
# own default plus a few common names so the brief is consistent.
BRIEF_WATCHLIST = ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD"]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M ET")


def latest_report_dir(parent: Path) -> Path | None:
    """The daily workflow writes into YYYYMMDD-HHMMSS/ subdirs; pick the newest."""
    if not parent.exists():
        return None
    subdirs = [d for d in parent.iterdir() if d.is_dir() and d.name[:8].isdigit()]
    if not subdirs:
        return None
    return sorted(subdirs, key=lambda d: d.name)[-1]


def run_pipeline(report_dir: Path, sizing_mode: str) -> tuple[bool, str]:
    """Run the full quant.py daily pipeline. Returns (ok, stderr_tail).

    Sizing mode is now derived from the macro regime by `derive_sizing_mode`
    (called in main() after fetching the brief) — see REGIME_TO_SIZING for
    the mapping. NAV is not passed here: the risk report exposes options_bp /
    cash_only and pretrade + action_plan derive account_nav from that
    automatically (see common.derive_live_account_nav, pitfall #28)."""
    print(f"[{now_str()}] running quant.py daily (sizing-mode={sizing_mode}) -> {report_dir}", file=sys.stderr)
    try:
        proc = subprocess.run(
            [
                PY, "quant.py",
                "--config", str(CONFIG),
                "daily",
                "--profile", "planning",
                "--skip-brief",  # we compose the Telegram message ourselves below
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


def fetch_brief() -> str:
    """Run daily_brief.py WITHOUT --send so we get the text to embed."""
    print(f"[{now_str()}] fetching daily brief", file=sys.stderr)
    try:
        proc = subprocess.run(
            [PY, "daily_brief.py", "--watchlist", *BRIEF_WATCHLIST],
            capture_output=True, text=True, timeout=180,
            cwd=str(SCRIPTS),
        )
    except subprocess.TimeoutExpired:
        return "(brief fetch timed out)"
    if proc.returncode != 0:
        return f"(brief fetch failed: {proc.stderr.strip()[:200]})"
    return proc.stdout.strip()


def format_alerts(report_dir: Path | None) -> str:
    if not report_dir:
        return ""
    alerts_path = report_dir / "alerts.json"
    if not alerts_path.exists():
        return ""
    try:
        data = json.loads(alerts_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"  (alerts parse failed: {e})", file=sys.stderr)
        return ""

    summary = data.get("summary", {})
    total = int(summary.get("total", 0))
    if total == 0:
        return ""

    lines = [f"\n📣 {total} ALERT{'S' if total != 1 else ''} (H:{summary.get('high', 0)} M:{summary.get('medium', 0)} L:{summary.get('low', 0)})"]
    for a in data.get("alerts", [])[:5]:
        lines.append(f"  • [{a.get('priority', '?')}] {a.get('title', '')} — {a.get('detail', '')}")
    return "\n".join(lines)


def format_top_tickets(report_dir: Path | None, n: int = 3) -> str:
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

    tickets = data.get("tickets", [])
    if not tickets:
        return ""

    lines = [f"\n🎫 TOP {min(len(tickets), n)} TICKET{'S' if len(tickets) != 1 else ''}"]
    for t in tickets[:n]:
        lines.append(
            f"  • {t.get('decision', '?'):7s} {t.get('ticker', '?'):6s} "
            f"{t.get('strategy', '?'):9s} {t.get('expiration', '?')} {t.get('strikes', '?'):>10s} "
            f"limit={t.get('limit_credit', '?')} floor={t.get('do_not_chase_below', '?')}"
        )
    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    if not message.strip():
        return False
    if os.environ.get("MORNING_DRY_RUN"):
        # Print to stdout for inspection; the cron will still see a clean exit
        # so this is safe to leave in production. Set the env var to debug
        # the message composition without spamming Telegram.
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
    """Best-effort failure alert; ignore if it also fails (cron will still report rc!=0)."""
    msg = f"⚠️ Morning workflow FAILED at {stage}\n{detail[:300]}"
    try:
        send_telegram(msg)
    except Exception:
        pass


def main() -> int:
    if not CONFIG.exists() or CONFIG.name == "config.example.json":
        print(f"cron_morning_workflow requires a production config.json; got {CONFIG}", file=sys.stderr)
        return 1
    today = datetime.now().strftime("%Y-%m-%d")
    report_dir = PROJECT / "reports" / f"cron-{today}"
    report_dir.mkdir(parents=True, exist_ok=True)

    # 1. Brief (fetched first so we can derive --sizing-mode from the live
    #    macro regime before the pipeline runs — saves a duplicate API call
    #    and guarantees the Telegram message + the pipeline agree on regime)
    brief = fetch_brief()
    sizing_mode, verdict = derive_sizing_mode(brief)
    if verdict:
        print(f"[{now_str()}] macro regime={verdict} → sizing-mode={sizing_mode}", file=sys.stderr)
    else:
        print(f"[{now_str()}] could not parse macro regime from brief → sizing-mode={sizing_mode} (default)", file=sys.stderr)

    # 2. Pipeline (sizing_mode derived from the brief's regime verdict)
    ok, err = run_pipeline(report_dir, sizing_mode)
    if not ok:
        send_failure_alert("pipeline", err)
        print(f"⚠️ Morning workflow failed at pipeline: {err}", file=sys.stderr)
        return 1

    latest = latest_report_dir(report_dir)
    if not latest:
        send_failure_alert("manifest", "planning pipeline produced no report directory")
        return 1
    brief_path = latest / "morning_brief.txt"
    brief_path.write_text(brief)
    RUN_POINTER.parent.mkdir(parents=True, exist_ok=True)
    pointer = {
        "profile": "planning",
        "created_at": datetime.now().isoformat(),
        "run_dir": str(latest),
        "brief": str(brief_path),
        "sizing_mode": sizing_mode,
        "regime": verdict,
    }
    pointer_tmp = RUN_POINTER.with_suffix(".tmp")
    pointer_tmp.write_text(json.dumps(pointer, indent=2))
    pointer_tmp.replace(RUN_POINTER)

    # 3. Planning alerts (compose on top of the brief we already captured)
    alerts_text = format_alerts(latest)

    # 4. Compose + truncate
    composed = (brief + alerts_text).strip()
    if not composed:
        composed = "(empty morning brief — no content produced)"
    if len(composed) > TELEGRAM_LIMIT:
        composed = composed[: TELEGRAM_LIMIT - 60] + "\n... (truncated, full report at " + str(latest) + ")"

    # 5. Send
    if not send_telegram(composed):
        print("⚠️ Telegram send failed", file=sys.stderr)
        return 1

    print(f"✅ Morning workflow delivered {now_str()} ({len(composed)} chars); reports at {latest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
