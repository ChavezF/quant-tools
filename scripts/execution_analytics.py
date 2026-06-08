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
        "avg_credit_improvement": 0.0,
        "floor_violations": 0,
        "_improvements": [],
    }


def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    improvements = bucket.pop("_improvements", [])
    bucket["fill_rate"] = round(bucket["matched"] / bucket["tickets"] * 100, 1) if bucket["tickets"] else 0.0
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

    rows = []
    by_strategy = defaultdict(empty_bucket)
    by_grade = defaultdict(empty_bucket)

    for ticket_id, ticket in tickets.items():
        match = matched_by_ticket.get(ticket_id, {})
        status = match.get("status", "UNMATCHED")
        planned = as_float(ticket.get("limit_credit"))
        floor = as_float(ticket.get("do_not_chase_below"))
        fill_price = as_float(match.get("fill_price"))
        improvement = None if planned is None or fill_price is None else round(fill_price - planned, 3)
        floor_violation = bool(floor is not None and fill_price is not None and fill_price < floor)
        row = {
            "ticket_id": ticket_id,
            "ticker": ticket.get("ticker"),
            "strategy": ticket.get("strategy"),
            "execution_grade": ticket.get("execution_grade"),
            "status": status,
            "planned_limit_credit": planned,
            "fill_price": fill_price,
            "credit_improvement": improvement,
            "floor_violation": floor_violation,
        }
        rows.append(row)

        for bucket in (by_strategy[str(ticket.get("strategy") or "UNKNOWN")], by_grade[str(ticket.get("execution_grade") or "UNKNOWN")]):
            bucket["tickets"] += 1
            if status == "MATCHED":
                bucket["matched"] += 1
            if improvement is not None:
                bucket["_improvements"].append(improvement)
            if floor_violation:
                bucket["floor_violations"] += 1

    matched = [row for row in rows if row["status"] == "MATCHED"]
    improvements = [row["credit_improvement"] for row in rows if row["credit_improvement"] is not None]
    summary = {
        "tickets": len(rows),
        "matched": len(matched),
        "unmatched": len(rows) - len(matched),
        "fill_rate": round(len(matched) / len(rows) * 100, 1) if rows else 0.0,
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
    print(
        f"  Tickets={summary['tickets']} matched={summary['matched']} "
        f"fill={summary['fill_rate']:.1f}% avg_credit_vs_plan={summary['avg_credit_improvement']:+.3f}"
    )
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
