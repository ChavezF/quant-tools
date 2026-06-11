#!/usr/bin/env python3.12
"""Build human-reviewable execution tickets from an action plan."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def setup_key(value: dict[str, Any]) -> str:
    return "|".join(
        str(value.get(key) or "").strip().upper()
        for key in ("ticker", "strategy", "expiration", "strikes")
    )


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


def target_quantity(action: dict[str, Any], candidate: dict[str, Any]) -> float:
    for value in (
        action.get("target_quantity"),
        action.get("quantity"),
        candidate.get("target_quantity"),
        candidate.get("quantity"),
        candidate.get("contracts"),
    ):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 1.0


def session_expiry(issued_at: str) -> str:
    try:
        eastern = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        # Windows Python installations do not always bundle IANA tzdata.
        eastern = timezone(timedelta(hours=-4))
    issued = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=eastern)
    local = issued.astimezone(eastern)
    expiry = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local >= expiry:
        expiry += timedelta(days=1)
        while expiry.weekday() >= 5:
            expiry += timedelta(days=1)
    return expiry.isoformat()


def build_ticket(action: dict[str, Any], batch_id: str = "") -> dict[str, Any]:
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
            batch_id,
        )
    )
    issued_at = batch_id or datetime.now().astimezone().isoformat()
    return {
        "ticket_id": f"QTK-{hashlib.sha1(ticket_key.encode()).hexdigest()[:10].upper()}",
        "issued_at": issued_at,
        "execution_batch_id": batch_id or issued_at,
        "expires_at": session_expiry(issued_at),
        "lifecycle_status": "READY",
        "ticker": action.get("ticker"),
        "strategy": strategy,
        "decision": action.get("action_decision"),
        "size_multiplier": action.get("action_size_multiplier"),
        "target_quantity": target_quantity(action, candidate),
        "order_action": order_action(strategy),
        "expiration": candidate.get("expiration"),
        "dte": candidate.get("dte"),
        "strikes": strikes_text(candidate),
        "limit_credit": execution.get("suggested_limit_credit"),
        "do_not_chase_below": execution.get("do_not_chase_below"),
        "execution_grade": execution.get("execution_grade"),
        "score": action.get("score"),
        "verdict": candidate.get("verdict"),
        "pop_pct": candidate.get("pop_pct"),
        "ann_roc_pct": candidate.get("ann_roc_pct"),
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


def build_ticket_report(
    plan: dict[str, Any],
    include_reduce: bool = True,
    active_tickets: list[dict[str, Any]] | None = None,
    suppress_duplicates: bool = True,
) -> dict[str, Any]:
    allowed = {"APPROVE", "REDUCE"} if include_reduce else {"APPROVE"}
    batch_id = str(plan.get("created_at") or "")
    active_by_setup: dict[str, list[str]] = {}
    for active in active_tickets or []:
        active_by_setup.setdefault(setup_key(active), []).append(str(active.get("ticket_id")))
    tickets = []
    suppressed = []
    for action in plan.get("actions", []):
        if action.get("action_decision") not in allowed:
            continue
        ticket = build_ticket(action, batch_id=batch_id)
        duplicate_ids = active_by_setup.get(setup_key(ticket), [])
        if duplicate_ids and suppress_duplicates:
            suppressed.append(
                {
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                    "expiration": ticket.get("expiration"),
                    "strikes": ticket.get("strikes"),
                    "active_ticket_ids": duplicate_ids,
                    "reason": "equivalent active execution ticket",
                }
            )
            continue
        tickets.append(ticket)
    tickets.sort(key=lambda row: (0 if row["decision"] == "APPROVE" else 1, -(float(row.get("score") or 0))))
    return {"tickets": tickets, "suppressed_duplicates": suppressed}


def build_tickets(plan: dict[str, Any], include_reduce: bool = True) -> list[dict[str, Any]]:
    return build_ticket_report(plan, include_reduce=include_reduce)["tickets"]


def print_tickets(tickets: list[dict[str, Any]]) -> None:
    print(f"\n{'#'*78}")
    print("# EXECUTION TICKETS")
    print(f"{'#'*78}\n")
    if not tickets:
        print("  No tickets.")
        return
    for i, ticket in enumerate(tickets, 1):
        print(f"  [{i}] {ticket['decision']} {ticket['ticker']} {ticket['strategy']} {ticket['expiration']} {ticket['strikes']}")
        print(
            f"      Action: {ticket['order_action']}  "
            f"Quantity: {ticket['target_quantity']:g}  Size: {ticket['size_multiplier']}"
        )
        print(f"      Limit credit: {ticket['limit_credit']}  Floor: {ticket['do_not_chase_below']}  Exec: {ticket['execution_grade']}")
        print(f"      Max loss: ${float(ticket.get('max_loss') or 0):,.2f}  Score: {float(ticket.get('score') or 0):.1f}")
        print(f"      {ticket['safety']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True, help="Path to action_plan --json output")
    ap.add_argument("--approve-only", action="store_true")
    ap.add_argument("--db")
    ap.add_argument("--allow-duplicates", action="store_true")
    ap.add_argument("--pending-expiry-hours", type=float, default=24)
    ap.add_argument("--partial-review-hours", type=float, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text())
    active_tickets = []
    if args.db:
        from storage import apply_lifecycle_policy, connect, load_active_tickets

        con = connect(args.db)
        try:
            apply_lifecycle_policy(
                con,
                pending_expiry_hours=args.pending_expiry_hours,
                partial_review_hours=args.partial_review_hours,
            )
            active_tickets = load_active_tickets(con)
        finally:
            con.close()
    report = build_ticket_report(
        plan,
        include_reduce=not args.approve_only,
        active_tickets=active_tickets,
        suppress_duplicates=not args.allow_duplicates,
    )
    tickets = report["tickets"]
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print_tickets(tickets)
    if report["suppressed_duplicates"]:
        print(f"\n  Suppressed duplicate setups: {len(report['suppressed_duplicates'])}")


if __name__ == "__main__":
    main()
