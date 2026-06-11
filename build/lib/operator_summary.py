#!/usr/bin/env python3.12
"""Create a concise, send-ready morning review from workflow reports."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from common import read_json


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
    execution_history: dict[str, Any] | None = None,
) -> str:
    reconciliation_wrapper = reconciliation or {}
    lifecycle_counts = reconciliation_wrapper.get("ticket_lifecycle", {})
    reconciliation = reconciliation_wrapper.get("reconciliation", reconciliation_wrapper)
    recon_summary = reconciliation.get("summary", {})
    execution_summary = (execution_analytics or {}).get("summary", {})
    execution_history = execution_history or {}
    execution_history_summary = execution_history.get("summary", {})
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
        f"- Planning candidates: {summary.get('approve', 0) + summary.get('reduce', 0)}",
        f"- Ready for review: {lifecycle_counts.get('READY', len(tickets.get('tickets', [])))}",
        f"- Submitted orders: {lifecycle_counts.get('SUBMITTED', 0)}",
        f"- Working orders: {lifecycle_counts.get('WORKING', 0)}",
        f"- Partial fills: {lifecycle_counts.get('PARTIAL', 0)}",
        f"- Filled orders: {int(lifecycle_counts.get('FILLED', 0) or 0) + int(lifecycle_counts.get('OVERFILLED', 0) or 0)}",
        f"- Active execution tickets: {recon_summary.get('active_tickets', 0)}",
        f"- Tickets auto-expired: {recon_summary.get('expired_tickets', 0)}",
        f"- Stale partial fills: {recon_summary.get('stale_partial_tickets', 0)}",
        f"- Duplicate active setups: {recon_summary.get('duplicate_active_setups', 0)}",
        f"- Unmatched tickets: {recon_summary.get('unmatched_tickets', 0)}",
        f"- Partially filled tickets: {recon_summary.get('partial_tickets', 0)}",
        f"- Overfilled tickets: {recon_summary.get('overfilled_tickets', 0)}",
        f"- Broker position exceptions: {recon_summary.get('position_exceptions', recon_summary.get('missing_positions', 0))}",
        f"- Fill rate: {'NO_SUBMITTED_HISTORY' if execution_summary.get('status') == 'NO_SUBMITTED_HISTORY' else f'{float(execution_summary.get('fill_rate', 0) or 0):.1f}%'}",
        f"- Quantity fill rate: {float(execution_summary.get('quantity_fill_rate', 0) or 0):.1f}%",
        f"- Average credit vs plan: {float(execution_summary.get('avg_credit_improvement', 0) or 0):+.3f}",
        f"- Execution fees: ${float(execution_summary.get('total_fees', 0) or 0):,.2f}",
        f"- Average fill delay: {float(execution_summary.get('avg_fill_delay_seconds', 0) or 0):,.0f} seconds",
        f"- Execution floor violations: {execution_summary.get('floor_violations', 0)}",
        f"- Durable execution samples: {execution_history_summary.get('count', 0)}",
        f"- Durable fees per contract: ${float(execution_history_summary.get('fees_per_contract', 0) or 0):,.2f}",
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
        "## Execution Attribution",
        "",
    ]
    strategy_adjustments = execution_history.get("strategy_adjustments", {})
    if not strategy_adjustments:
        lines.append("- Insufficient durable execution history.")
    for strategy, row in strategy_adjustments.items():
        lines.append(
            f"- **{strategy}** {row.get('signal', 'UNKNOWN')}: "
            f"score {float(row.get('score_adjustment', 0) or 0):+.1f}, "
            f"size x{float(row.get('size_multiplier', 1) or 1):.2f}, "
            f"n={row.get('sample_size', 0)}"
        )
    lines.extend([
        "",
        "## Candidate Shortlist",
        "",
    ])
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

    lines.extend(["", "## Ready for Review", ""])
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
        execution_history=read_json(base / "execution_history.json"),
    )
    output.write_text(text)
    print(output)


if __name__ == "__main__":
    main()
