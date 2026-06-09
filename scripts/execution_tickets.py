#!/usr/bin/env python3.12
"""Build human-reviewable execution tickets from an action plan."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def strikes_text(candidate: dict[str, Any]) -> str:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "BULL_PUT":
        return f"{candidate.get('short_strike')}/{candidate.get('long_strike')}"
    if candidate.get("strike") is not None:
        return str(candidate.get("strike"))
    return "-"


def order_action(strategy: str) -> str:
    strategy = strategy.upper()
    if strategy in {"CSP", "CC"}:
        return "SELL_TO_OPEN"
    if strategy == "BULL_PUT":
        return "SELL_SPREAD_TO_OPEN"
    return "REVIEW"


def build_ticket(action: dict[str, Any]) -> dict[str, Any]:
    candidate = action.get("candidate", {})
    execution = candidate.get("execution", {})
    strategy = str(action.get("strategy") or candidate.get("strategy") or "").upper()
    allocation = action.get("portfolio_allocation", {})
    ticket_key = "|".join(
        str(value or "")
        for value in (
            action.get("ticker"),
            strategy,
            candidate.get("expiration"),
            strikes_text(candidate),
            action.get("score"),
        )
    )
    return {
        "ticket_id": f"QTK-{hashlib.sha1(ticket_key.encode()).hexdigest()[:10].upper()}",
        "ticker": action.get("ticker"),
        "strategy": strategy,
        "decision": action.get("action_decision"),
        "size_multiplier": action.get("action_size_multiplier"),
        "order_action": order_action(strategy),
        "expiration": candidate.get("expiration"),
        "dte": candidate.get("dte"),
        "strikes": strikes_text(candidate),
        "limit_credit": execution.get("suggested_limit_credit"),
        "do_not_chase_below": execution.get("do_not_chase_below"),
        "execution_grade": execution.get("execution_grade"),
        "score": action.get("score"),
        "max_loss": action.get("max_loss"),
        "capital_required": action.get("capital_required"),
        "portfolio_allocation": allocation,
        "rationale": {
            "profile": action.get("profile_note"),
            "correlation": action.get("correlation", {}).get("note"),
            "adaptive_sizing": action.get("adaptive_sizing", {}).get("note"),
            "calibrated_min_score": action.get("feedback_calibration", {}).get("recommended_min_score"),
            "risk_checks_failed": [c["name"] for c in action.get("checks", []) if not c.get("ok")],
            "allocation_rank": allocation.get("rank"),
            "allocation_objective_score": allocation.get("objective_score"),
            "allocation_tail_loss": allocation.get("tail_loss"),
        },
        "safety": "Review manually. Do not place orders without explicit confirmation.",
    }


def build_tickets(plan: dict[str, Any], include_reduce: bool = True) -> list[dict[str, Any]]:
    allowed = {"APPROVE", "REDUCE"} if include_reduce else {"APPROVE"}
    tickets = [build_ticket(action) for action in plan.get("actions", []) if action.get("action_decision") in allowed]
    tickets.sort(key=lambda row: (0 if row["decision"] == "APPROVE" else 1, -(float(row.get("score") or 0))))
    return tickets


def print_tickets(tickets: list[dict[str, Any]]) -> None:
    print(f"\n{'#'*78}")
    print("# EXECUTION TICKETS")
    print(f"{'#'*78}\n")
    if not tickets:
        print("  No tickets.")
        return
    for i, ticket in enumerate(tickets, 1):
        print(f"  [{i}] {ticket['decision']} {ticket['ticker']} {ticket['strategy']} {ticket['expiration']} {ticket['strikes']}")
        print(f"      Action: {ticket['order_action']}  Size: {ticket['size_multiplier']}")
        print(f"      Limit credit: {ticket['limit_credit']}  Floor: {ticket['do_not_chase_below']}  Exec: {ticket['execution_grade']}")
        print(f"      Max loss: ${float(ticket.get('max_loss') or 0):,.2f}  Score: {float(ticket.get('score') or 0):.1f}")
        print(f"      {ticket['safety']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="Path to action_plan --json output")
    ap.add_argument("--approve-only", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text())
    tickets = build_tickets(plan, include_reduce=not args.approve_only)
    if args.json:
        print(json.dumps({"tickets": tickets}, indent=2, default=str))
        return
    print_tickets(tickets)


if __name__ == "__main__":
    main()
