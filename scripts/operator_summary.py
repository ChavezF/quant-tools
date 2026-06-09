#!/usr/bin/env python3.12
"""Create a concise, send-ready morning review from workflow reports."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def build_summary(
    plan: dict[str, Any],
    alerts: dict[str, Any],
    tickets: dict[str, Any],
    analytics: dict[str, Any],
    feedback: dict[str, Any],
    reconciliation: dict[str, Any] | None = None,
    execution_analytics: dict[str, Any] | None = None,
    database_maintenance: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
    scenario_stress: dict[str, Any] | None = None,
    allocation: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    drift: dict[str, Any] | None = None,
) -> str:
    reconciliation = reconciliation or {}
    reconciliation = reconciliation.get("reconciliation", reconciliation)
    recon_summary = reconciliation.get("summary", {})
    execution_summary = (execution_analytics or {}).get("summary", {})
    database_maintenance = database_maintenance or {}
    health = health or {}
    stress_summary = (scenario_stress or {}).get("summary", {})
    allocation_summary = (allocation or {}).get("summary", {})
    validation_summary = (validation or {}).get("summary", {})
    drift = drift or {}
    drift_summary = drift.get("summary", {})
    drift_comparison = drift.get("comparison", {})
    database_status = "NOT RUN" if not database_maintenance else ("OK" if database_maintenance.get("ok") else "FAILED")
    health_status = "NOT RUN" if not health else ("OK" if health.get("ok") else "FAILED")
    summary = plan.get("summary", {})
    overall = analytics.get("overall", {})
    drawdown = analytics.get("drawdown", {})
    lines = [
        "# Quant Tools Morning Review",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Decision Snapshot",
        "",
        f"- Approved: {summary.get('approve', 0)}",
        f"- Reduced: {summary.get('reduce', 0)}",
        f"- Rejected: {summary.get('reject', 0)}",
        f"- High-priority alerts: {alerts.get('summary', {}).get('high', 0)}",
        f"- Execution tickets: {len(tickets.get('tickets', []))}",
        f"- Unmatched tickets: {recon_summary.get('unmatched_tickets', 0)}",
        f"- Broker position exceptions: {recon_summary.get('position_exceptions', recon_summary.get('missing_positions', 0))}",
        f"- Fill rate: {float(execution_summary.get('fill_rate', 0) or 0):.1f}%",
        f"- Average credit vs plan: {float(execution_summary.get('avg_credit_improvement', 0) or 0):+.3f}",
        f"- Execution floor violations: {execution_summary.get('floor_violations', 0)}",
        f"- Database integrity: {database_status}",
        f"- Database backup: {database_maintenance.get('backup') or 'not created'}",
        f"- Health checks: {health_status}",
        f"- Worst stress scenario: {stress_summary.get('worst_scenario') or 'NOT RUN'}",
        f"- Worst stress P&L: ${float(stress_summary.get('worst_pnl', 0) or 0):,.2f} "
        f"({float(stress_summary.get('worst_pnl_pct_nav', 0) or 0):.2f}% NAV)",
        f"- Basket selected: {allocation_summary.get('selected', 0)} "
        f"of {allocation_summary.get('eligible', 0)} eligible",
        f"- Capital allocated: ${float(allocation_summary.get('capital_allocated', 0) or 0):,.2f} "
        f"({float(allocation_summary.get('capital_utilization_pct', 0) or 0):.1f}% of budget)",
        f"- Tail-loss budget used: {float(allocation_summary.get('tail_budget_utilization_pct', 0) or 0):.1f}%",
        f"- Walk-forward validation: {validation_summary.get('status') or 'NOT RUN'} "
        f"({float(validation_summary.get('profitable_fold_pct', 0) or 0):.1f}% profitable folds)",
        f"- OOS expectancy: ${float(validation_summary.get('avg_oos_expectancy', 0) or 0):,.2f}",
        f"- Performance drift: {drift_summary.get('status') or 'NOT RUN'} "
        f"({drift_summary.get('severity') or 'N/A'})",
        f"- Recent expectancy change: ${float(drift_comparison.get('expectancy_change', 0) or 0):+,.2f}",
        f"- Score threshold shift: {float(drift_summary.get('score_threshold_shift', 0) or 0):+.1f}",
        "",
        "## Realized Edge",
        "",
        f"- Closed trades: {overall.get('count', 0)}",
        f"- Win rate: {float(overall.get('win_rate', 0) or 0):.1f}%",
        f"- Expectancy: ${float(overall.get('expectancy', 0) or 0):,.2f}",
        f"- Total realized P&L: ${float(overall.get('total_pnl', 0) or 0):,.2f}",
        f"- Max drawdown: ${float(drawdown.get('max_drawdown', 0) or 0):,.2f}",
        f"- Recommended minimum score: {float(feedback.get('recommended_min_score', 0) or 0):.1f}",
        "",
        "## Candidate Shortlist",
        "",
    ]
    actionable = [row for row in plan.get("actions", []) if row.get("action_decision") in {"APPROVE", "REDUCE"}]
    if not actionable:
        lines.append("- No actionable candidates.")
    for row in actionable[:10]:
        execution = row.get("candidate", {}).get("execution", {})
        lines.append(
            f"- **{row.get('action_decision')} {row.get('ticker')} {row.get('strategy')}** "
            f"score {float(row.get('score', 0) or 0):.1f}, size x{float(row.get('action_size_multiplier', 0) or 0):.2f}, "
            f"limit {execution.get('suggested_limit_credit')}, floor {execution.get('do_not_chase_below')}"
        )

    lines.extend(["", "## Execution Queue", ""])
    ticket_rows = tickets.get("tickets", [])
    if not ticket_rows:
        lines.append("- No tickets.")
    for ticket in ticket_rows[:10]:
        allocation_rank = ticket.get("portfolio_allocation", {}).get("rank")
        rank_text = f"rank {allocation_rank}, " if allocation_rank else ""
        lines.append(
            f"- `{ticket.get('ticket_id')}` {ticket.get('ticker')} {ticket.get('strategy')} "
            f"{ticket.get('expiration')} {ticket.get('strikes')} at {ticket.get('limit_credit')} "
            f"({rank_text}size x{float(ticket.get('size_multiplier', 0) or 0):.2f})"
        )

    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Review every ticket manually.",
            "- Do not place orders without explicit confirmation.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-dir", required=True)
    ap.add_argument("--output")
    args = ap.parse_args()

    base = Path(args.report_dir)
    output = Path(args.output) if args.output else base / "operator_summary.md"
    text = build_summary(
        read_json(base / "plan.json"),
        read_json(base / "alerts.json"),
        read_json(base / "tickets.json"),
        read_json(base / "analytics.json"),
        read_json(base / "feedback.json"),
        read_json(base / "reconciliation.json"),
        read_json(base / "execution_analytics.json"),
        read_json(base / "database_maintenance.json"),
        read_json(base / "health.json"),
        read_json(base / "scenario_stress.json"),
        read_json(base / "allocation.json"),
        read_json(base / "validation.json"),
        read_json(base / "drift.json"),
    )
    output.write_text(text)
    print(output)


if __name__ == "__main__":
    main()
