#!/usr/bin/env python3.12
"""Pure report and state helpers shared by the Hermes cron wrappers."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from common import atomic_write_json


DEFAULT_WATCHLIST = ("SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD")
TELEGRAM_LIMIT = 4000


def latest_report_dir(parent: Path) -> Path | None:
    if not parent.exists():
        return None
    subdirs = [path for path in parent.iterdir() if path.is_dir() and path.name[:8].isdigit()]
    return max(subdirs, key=lambda path: path.name, default=None)


def read_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_run_pointer(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def read_planning_pointer(
    path: Path,
    *,
    today: date | None = None,
) -> dict[str, Any] | None:
    payload = read_report(path)
    if payload.get("profile") != "planning":
        return None
    try:
        created = datetime.fromisoformat(str(payload.get("created_at")))
    except ValueError:
        return None
    if created.date() != (today or datetime.now().date()):
        return None
    run_dir = Path(str(payload.get("run_dir") or ""))
    brief = Path(str(payload.get("brief") or ""))
    if not run_dir.is_dir() or not brief.is_file():
        return None
    return payload


def planning_brief(path: Path, *, today: date | None = None) -> str | None:
    payload = read_planning_pointer(path, today=today)
    if not payload:
        return None
    try:
        return Path(str(payload["brief"])).read_text()
    except OSError:
        return None


def format_alerts(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    total = int(summary.get("total", 0) or 0)
    if total == 0:
        return ""
    lines = [
        f"\nALERTS ({total}; H:{summary.get('high', 0)} "
        f"M:{summary.get('medium', 0)} L:{summary.get('low', 0)})"
    ]
    for alert in report.get("alerts", [])[:5]:
        lines.append(
            f"  [{alert.get('priority', '?')}] {alert.get('title', '')}: "
            f"{alert.get('detail', '')}"
        )
    return "\n".join(lines)


def format_management(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    open_count = int(summary.get("open_trades", 0) or 0)
    if open_count == 0:
        return ""
    lines = [
        f"\nOPEN POSITIONS ({open_count}): {summary.get('close', 0)} close, "
        f"{summary.get('roll_or_close', 0)} roll, {summary.get('review', 0)} review, "
        f"{summary.get('hold', 0)} hold"
    ]
    for action in report.get("actions", [])[:5]:
        dte = action.get("dte")
        pnl = action.get("unrealized_pnl_pct")
        dte_text = f"{dte}d" if dte is not None else "-"
        pnl_text = f"{float(pnl):+.0f}%" if pnl is not None else "-"
        reason = (action.get("reasons") or [""])[0]
        lines.append(
            f"  {action.get('ticker', '?')} {action.get('strategy', '?')} "
            f"DTE={dte_text} P&L={pnl_text} -> {action.get('action', '?')}: {reason}"
        )
        roll = action.get("roll_proposal") or {}
        if roll.get("status") == "CREDIT_AVAILABLE":
            lines.append(
                f"    roll {roll.get('to_expiration')} {roll.get('to_strike')} "
                f"for net credit >= {roll.get('net_credit')}"
            )
    return "\n".join(lines)


def format_executable_tickets(report: dict[str, Any], limit: int = 3) -> str:
    tickets = report.get("tickets", [])
    if not tickets:
        return "\nNO CANDIDATES TODAY"
    actionable = [
        ticket for ticket in tickets
        if str(ticket.get("decision", "")).upper() in {"APPROVE", "STRONG"}
    ]
    reduced = [
        ticket for ticket in tickets
        if str(ticket.get("decision", "")).upper() == "REDUCE"
    ]
    lines = [f"\nCANDIDATES ({len(tickets)})"]
    if actionable:
        lines.append(f"  EXECUTABLE ({len(actionable)} approved):")
        for ticket in actionable[:limit]:
            lines.append(
                f"    {ticket.get('ticker', '?')} {ticket.get('strategy', '?')} "
                f"{ticket.get('expiration', '?')} {ticket.get('strikes', '?')} "
                f"credit>={ticket.get('limit_credit', '?')} "
                f"floor={ticket.get('do_not_chase_below', '?')} score={ticket.get('score', '?')}"
            )
    if reduced:
        lines.append(f"  REDUCED SIZE ({len(reduced)} flagged):")
        for ticket in reduced[:limit]:
            rationale = ticket.get("rationale") or {}
            reasons = [
                str(value)
                for value in (
                    rationale.get("adaptive_sizing"),
                    rationale.get("profile"),
                    rationale.get("correlation"),
                )
                if value
            ]
            lines.append(
                f"    {ticket.get('ticker', '?')} {ticket.get('strategy', '?')} "
                f"{ticket.get('expiration', '?')} {ticket.get('strikes', '?')} "
                f"size x{ticket.get('size_multiplier', '?')} score={ticket.get('score', '?')}: "
                f"{' | '.join(reasons)[:80] or 'see plan for details'}"
            )
    if not actionable and not reduced:
        lines.append(f"  All {len(tickets)} candidates are HOLD.")
    return "\n".join(lines)


def truncate_message(message: str, report_dir: Path | None, limit: int = TELEGRAM_LIMIT) -> str:
    if len(message) <= limit:
        return message
    suffix = f"\n... (truncated; full report at {report_dir})"
    return message[: max(0, limit - len(suffix))] + suffix


def compose_planning_message(
    brief: str,
    alerts: dict[str, Any],
    report_dir: Path | None,
    *,
    limit: int = TELEGRAM_LIMIT,
) -> str:
    message = (brief + format_alerts(alerts)).strip() or "(empty morning brief)"
    return truncate_message(message, report_dir, limit)


def compose_executable_message(
    *,
    timestamp: str,
    regime: str | None,
    sizing_mode: str,
    management: dict[str, Any],
    tickets: dict[str, Any],
    report_dir: Path | None,
    limit: int = TELEGRAM_LIMIT,
) -> str:
    message = (
        f"10:30 EXECUTABLE SCAN - {timestamp}\n"
        f"Regime: {regime or 'UNKNOWN'} | sizing: {sizing_mode}"
        f"{format_management(management)}"
        f"{format_executable_tickets(tickets)}"
    ).strip()
    return truncate_message(message, report_dir, limit)
