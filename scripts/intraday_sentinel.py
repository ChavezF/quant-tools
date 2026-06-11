#!/usr/bin/env python3.12
"""Quiet intraday guard: mark, manage, and notify only on new risk states."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT, atomic_write_json, state_lock
from toolkit_config import add_config_argument, load_config, resolve_project_path


DEFAULT_STATE = PROJECT_ROOT / "state" / "intraday_sentinel.json"
SCRIPTS = Path(__file__).parent


def alertable_states(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    states = {}
    for row in report.get("actions", []):
        threat = row.get("strike_threat", {})
        if row.get("urgency") != "HIGH" and threat.get("status") not in {"THREAT", "BREACHED"}:
            continue
        payload = {
            "trade_id": row.get("trade_id"),
            "ticker": row.get("ticker"),
            "strategy": row.get("strategy"),
            "action": row.get("action"),
            "urgency": row.get("urgency"),
            "strike_status": threat.get("status"),
            "event_span": [
                f"{event.get('event_type')}:{event.get('date')}"
                for event in row.get("event_span", [])
            ],
            "roll_status": (row.get("roll_proposal") or {}).get("status"),
        }
        stable = json.dumps(payload, sort_keys=True, default=str)
        payload["fingerprint"] = hashlib.sha1(stable.encode()).hexdigest()
        states[str(row.get("trade_id"))] = payload
    return states


def compare_states(
    previous: dict[str, dict[str, Any]],
    current: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    changed = [
        row
        for trade_id, row in current.items()
        if previous.get(trade_id, {}).get("fingerprint") != row.get("fingerprint")
    ]
    resolved = [
        row
        for trade_id, row in previous.items()
        if trade_id not in current
    ]
    return {"changed": changed, "resolved": resolved}


def format_message(report: dict[str, Any], changed: list[dict[str, Any]]) -> str:
    if not changed:
        return ""
    by_id = {str(row.get("trade_id")): row for row in report.get("actions", [])}
    lines = [f"INTRADAY POSITION ALERT - {datetime.now().strftime('%Y-%m-%d %H:%M ET')}"]
    for state in changed:
        row = by_id.get(str(state.get("trade_id")), {})
        threat = row.get("strike_threat", {})
        lines.append(
            f"- {row.get('ticker')} {row.get('strategy')} -> {row.get('action')} "
            f"[{row.get('urgency')}]"
        )
        if threat.get("status") in {"THREAT", "BREACHED"}:
            lines.append(
                f"  {threat.get('status')}: spot {threat.get('spot')}, "
                f"short {threat.get('option_type')}{threat.get('short_strike')}, "
                f"{threat.get('sigma_distance')} sigma"
            )
        for event in row.get("event_span", []):
            lines.append(f"  Spans {event.get('event_type')} on {event.get('date')}")
        roll = row.get("roll_proposal") or {}
        if roll.get("status") == "CREDIT_AVAILABLE":
            lines.append(
                f"  Roll: {roll.get('to_expiration')} {roll.get('to_strike')} "
                f"for net credit >= {roll.get('net_credit')}"
            )
        if row.get("reasons"):
            lines.append(f"  {row['reasons'][0]}")
    return "\n".join(lines)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"states": {}}
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {"states": {}}
    return value if isinstance(value, dict) else {"states": {}}


def run_json(cmd: list[str], timeout: int = 300) -> dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(SCRIPTS))
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"command failed: {' '.join(cmd)}")
    return json.loads(proc.stdout)


def send_telegram(message: str) -> bool:
    if not message:
        return False
    proc = subprocess.run(
        ["hermes", "send", "--to", "telegram", message],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--journal")
    ap.add_argument("--db")
    ap.add_argument("--state-file", default=str(DEFAULT_STATE))
    ap.add_argument("--management-report", help="Use an existing report instead of live mark/manage")
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    journal = resolve_project_path(args.journal or cfg.get("journal", {}).get("path"))
    db = resolve_project_path(args.db or cfg.get("storage", {}).get("path"))
    if args.management_report:
        report = json.loads(Path(args.management_report).read_text())
    else:
        mark_cmd = [sys.executable, str(SCRIPTS / "mark_to_market.py"), "--journal", journal, "--json"]
        manage_cmd = [
            sys.executable,
            str(SCRIPTS / "position_management.py"),
            *(["--config", args.config] if args.config else []),
            "--journal",
            journal,
            "--json",
        ]
        if db:
            mark_cmd += ["--db", db]
            manage_cmd += ["--db", db]
        run_json(mark_cmd)
        report = run_json(manage_cmd)

    state_path = Path(args.state_file)
    with state_lock("intraday-sentinel"):
        previous = read_state(state_path)
        current = alertable_states(report)
        changes = compare_states(previous.get("states", {}), current)
        atomic_write_json(
            state_path,
            {
                "updated_at": datetime.now().isoformat(),
                "states": current,
            },
        )
    message = format_message(report, changes["changed"])
    sent = False
    if args.send and message:
        sent = send_telegram(message)
        if not sent:
            raise SystemExit("Telegram send failed")
    result = {
        "as_of": datetime.now().isoformat(),
        "changed": changes["changed"],
        "resolved": changes["resolved"],
        "message": message,
        "sent": sent,
        "quiet": not bool(message),
    }
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    elif message:
        print(message)
    else:
        print("No new intraday risk state.")


if __name__ == "__main__":
    main()
