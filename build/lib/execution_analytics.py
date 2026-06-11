#!/usr/bin/env python3.12
"""Analyze execution quality from tickets and reconciliation results."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def empty_bucket() -> dict[str, Any]:
    return {
        "tickets": 0,
        "matched": 0,
        "partial": 0,
        "target_quantity": 0.0,
        "filled_quantity": 0.0,
        "total_fees": 0.0,
        "avg_credit_improvement": 0.0,
        "floor_violations": 0,
        "_improvements": [],
    }


def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    improvements = bucket.pop("_improvements", [])
    bucket["fill_rate"] = round(bucket["matched"] / bucket["tickets"] * 100, 1) if bucket["tickets"] else 0.0
    bucket["quantity_fill_rate"] = (
        round(min(bucket["filled_quantity"], bucket["target_quantity"]) / bucket["target_quantity"] * 100, 1)
        if bucket["target_quantity"]
        else 0.0
    )
    bucket["avg_credit_improvement"] = round(sum(improvements) / len(improvements), 3) if improvements else 0.0
    return bucket


def ticket_map(tickets_report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(ticket.get("ticket_id")): ticket for ticket in tickets_report.get("tickets", []) if ticket.get("ticket_id")}


def build_execution_analytics(
    tickets_report: dict[str, Any],
    reconciliation_report: dict[str, Any],
) -> dict[str, Any]:
    tickets = ticket_map(tickets_report)
    reconciliation = reconciliation_report.get("reconciliation", reconciliation_report)
    matches = reconciliation.get("ticket_matches", [])
    matched_by_ticket = {str(row.get("ticket_id")): row for row in matches if row.get("ticket_id")}
    for ticket_id, match in matched_by_ticket.items():
        tickets.setdefault(
            ticket_id,
            {
                "ticket_id": ticket_id,
                "ticker": match.get("ticker"),
                "strategy": match.get("strategy"),
                "execution_grade": match.get("execution_grade"),
                "limit_credit": match.get("planned_limit_credit"),
                "do_not_chase_below": match.get("do_not_chase_below"),
                "target_quantity": match.get("target_quantity"),
            },
        )

    rows = []
    by_strategy = defaultdict(empty_bucket)
    by_grade = defaultdict(empty_bucket)

    submitted_statuses = {
        "SUBMITTED", "WORKING", "PARTIAL", "FILLED", "OVERFILLED",
        "REJECTED", "CANCEL_PENDING", "CANCELLED",
    }
    ready_count = 0
    skipped_unsubmitted = 0
    for ticket_id, ticket in tickets.items():
        match = matched_by_ticket.get(ticket_id, {})
        lifecycle = str(
            match.get("lifecycle_status") or ticket.get("lifecycle_status") or "READY"
        ).upper()
        submitted = bool(
            match.get("submitted_at")
            or match.get("broker_order_id")
            or ticket.get("submitted_at")
            or ticket.get("broker_order_id")
        )
        fill_evidence = str(match.get("status") or "").upper() in {
            "PARTIAL", "MATCHED", "OVERFILLED"
        }
        if lifecycle == "READY":
            ready_count += 1
        if not fill_evidence and (lifecycle not in submitted_statuses or not submitted):
            skipped_unsubmitted += 1
            continue
        status = match.get("status", "UNMATCHED")
        planned = as_float(ticket.get("limit_credit"))
        floor = as_float(ticket.get("do_not_chase_below"))
        fill_price = as_float(match.get("fill_price"))
        target_quantity = as_float(match.get("target_quantity")) or as_float(ticket.get("target_quantity")) or 1.0
        filled_quantity = as_float(match.get("filled_quantity")) or 0.0
        fees = as_float(match.get("fees")) or 0.0
        fill_delay_seconds = as_float(match.get("fill_delay_seconds"))
        improvement = None if planned is None or fill_price is None else round(fill_price - planned, 3)
        floor_violation = bool(floor is not None and fill_price is not None and fill_price < floor)
        row = {
            "ticket_id": ticket_id,
            "ticker": ticket.get("ticker"),
            "strategy": ticket.get("strategy"),
            "execution_grade": ticket.get("execution_grade"),
            "status": status,
            "lifecycle_status": lifecycle,
            "planned_limit_credit": planned,
            "fill_price": fill_price,
            "target_quantity": target_quantity,
            "filled_quantity": filled_quantity,
            "remaining_quantity": max(0.0, target_quantity - filled_quantity),
            "fill_count": int(match.get("fill_count") or 0),
            "fees": fees,
            "fill_delay_seconds": fill_delay_seconds,
            "credit_improvement": improvement,
            "floor_violation": floor_violation,
        }
        rows.append(row)

        for bucket in (by_strategy[str(ticket.get("strategy") or "UNKNOWN")], by_grade[str(ticket.get("execution_grade") or "UNKNOWN")]):
            bucket["tickets"] += 1
            bucket["target_quantity"] += target_quantity
            bucket["filled_quantity"] += min(filled_quantity, target_quantity)
            bucket["total_fees"] += fees
            if status in {"MATCHED", "OVERFILLED"}:
                bucket["matched"] += 1
            if status == "PARTIAL":
                bucket["partial"] += 1
            if improvement is not None:
                bucket["_improvements"].append(improvement)
            if floor_violation:
                bucket["floor_violations"] += 1

    matched = [row for row in rows if row["status"] in {"MATCHED", "OVERFILLED"}]
    partial = [row for row in rows if row["status"] == "PARTIAL"]
    improvements = [row["credit_improvement"] for row in rows if row["credit_improvement"] is not None]
    target_quantity = sum(row["target_quantity"] for row in rows)
    filled_quantity = sum(min(row["filled_quantity"], row["target_quantity"]) for row in rows)
    fill_delays = [row["fill_delay_seconds"] for row in rows if row["fill_delay_seconds"] is not None]
    summary = {
        "status": "AVAILABLE" if rows else "NO_SUBMITTED_HISTORY",
        "tickets": len(rows),
        "ready": ready_count,
        "unsubmitted_excluded": skipped_unsubmitted,
        "matched": len(matched),
        "partial": len(partial),
        "unmatched": sum(1 for row in rows if row["status"] == "UNMATCHED"),
        "fill_rate": round(len(matched) / len(rows) * 100, 1) if rows else 0.0,
        "quantity_fill_rate": round(filled_quantity / target_quantity * 100, 1) if target_quantity else 0.0,
        "target_quantity": round(target_quantity, 4),
        "filled_quantity": round(filled_quantity, 4),
        "total_fees": round(sum(row["fees"] for row in rows), 4),
        "avg_fill_delay_seconds": round(sum(fill_delays) / len(fill_delays), 1) if fill_delays else None,
        "avg_credit_improvement": round(sum(improvements) / len(improvements), 3) if improvements else 0.0,
        "floor_violations": sum(1 for row in rows if row["floor_violation"]),
    }
    return {
        "summary": summary,
        "by_strategy": {key: finalize_bucket(value) for key, value in sorted(by_strategy.items())},
        "by_execution_grade": {key: finalize_bucket(value) for key, value in sorted(by_grade.items())},
        "tickets": rows,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#'*78}\n# EXECUTION ANALYTICS\n{'#'*78}\n")
    if summary["status"] == "NO_SUBMITTED_HISTORY":
        print(f"  NO_SUBMITTED_HISTORY | ready={summary['ready']}")
        return
    print(
        f"  Tickets={summary['tickets']} matched={summary['matched']} "
        f"fill={summary['fill_rate']:.1f}% quantity_fill={summary['quantity_fill_rate']:.1f}% "
        f"avg_credit_vs_plan={summary['avg_credit_improvement']:+.3f}"
    )
    print(f"  Partial tickets: {summary['partial']}")
    print(f"  Floor violations: {summary['floor_violations']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickets", required=True)
    ap.add_argument("--reconciliation", required=True)
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_execution_analytics(
        json.loads(Path(args.tickets).read_text()),
        json.loads(Path(args.reconciliation).read_text()),
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
