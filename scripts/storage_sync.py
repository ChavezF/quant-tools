#!/usr/bin/env python3.12
"""Sync JSON workflow artifacts into the SQLite persistence store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from broker_reconciliation import build_reconciliation
from storage import (
    DEFAULT_DB_FILE,
    apply_ticket_lifecycle,
    apply_lifecycle_policy,
    connect,
    export_journal_state,
    fill_identity,
    insert_position_snapshot,
    load_active_tickets,
    load_fills_for_reconciliation,
    record_reconciliation,
    table_counts,
    ticket_lifecycle_counts,
    upsert_fills,
    upsert_tickets,
    upsert_trades,
)
from trade_journal import DEFAULT_STATE_FILE, load_state


def read_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def positions_from_portfolio(report: dict[str, Any]) -> list[dict[str, Any]]:
    return report.get("portfolio", {}).get("positions", []) or report.get("positions", [])


def sync_artifacts(
    db_path: str | Path,
    journal: dict[str, Any],
    tickets: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    lifecycle_events: list[dict[str, Any]] | None = None,
    *,
    pending_expiry_hours: float = 24,
    partial_review_hours: float = 4,
) -> dict[str, Any]:
    con = connect(db_path)
    try:
        imported = {
            "trades": upsert_trades(con, journal.get("trades", [])),
            "tickets": upsert_tickets(con, tickets),
            "positions": insert_position_snapshot(con, positions) if positions else 0,
            "fills": upsert_fills(con, fills),
        }
        active_tickets = load_active_tickets(con)
        reconciliation_fills = load_fills_for_reconciliation(
            con,
            [str(ticket["ticket_id"]) for ticket in active_tickets],
            [fill_identity(fill, index) for index, fill in enumerate(fills)],
        )
        report = build_reconciliation(
            journal,
            {"tickets": active_tickets},
            {
                "positions": positions,
                "fills": reconciliation_fills,
                "lifecycle_events": lifecycle_events or [],
            },
        )
        imported["ticket_lifecycle_updates"] = apply_ticket_lifecycle(
            con,
            report.get("ticket_matches", []),
        )
        lifecycle_policy = apply_lifecycle_policy(
            con,
            pending_expiry_hours=pending_expiry_hours,
            partial_review_hours=partial_review_hours,
        )
        lifecycle_counts = ticket_lifecycle_counts(con)
        report["ticket_lifecycle"] = lifecycle_counts
        report["lifecycle_policy"] = lifecycle_policy
        report["summary"]["active_tickets"] = sum(
            lifecycle_counts.get(status, 0) for status in ("PENDING", "PARTIAL")
        )
        report["summary"]["expired_tickets"] = len(lifecycle_policy["expired_tickets"])
        report["summary"]["stale_partial_tickets"] = len(lifecycle_policy["stale_partial_tickets"])
        report["summary"]["duplicate_active_setups"] = len(lifecycle_policy["duplicate_active_setups"])
        imported["reconciliation_run_id"] = record_reconciliation(con, report)
        counts = table_counts(con)
    finally:
        con.close()
    return {
        "imported": imported,
        "counts": counts,
        "ticket_lifecycle": lifecycle_counts,
        "reconciliation": report,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB_FILE))
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--tickets")
    ap.add_argument("--portfolio")
    ap.add_argument("--broker-snapshot")
    ap.add_argument("--output")
    ap.add_argument("--export-journal")
    ap.add_argument("--pending-expiry-hours", type=float, default=24)
    ap.add_argument("--partial-review-hours", type=float, default=4)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    journal = load_state(Path(args.journal))
    tickets = read_json(args.tickets).get("tickets", [])
    portfolio = read_json(args.portfolio)
    broker_snapshot = read_json(args.broker_snapshot)
    positions = positions_from_portfolio(portfolio) or broker_snapshot.get("positions", [])
    fills = broker_snapshot.get("fills", [])
    lifecycle_events = broker_snapshot.get("lifecycle_events", [])
    result = sync_artifacts(
        args.db,
        journal,
        tickets,
        positions,
        fills,
        lifecycle_events,
        pending_expiry_hours=args.pending_expiry_hours,
        partial_review_hours=args.partial_review_hours,
    )
    if args.export_journal:
        con = connect(args.db)
        try:
            Path(args.export_journal).write_text(json.dumps(export_journal_state(con), indent=2, default=str))
        finally:
            con.close()
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"SQLite sync complete: {args.db}")
        counts = result["counts"]
        print(f"  Trades={counts['trades']} Tickets={counts['tickets']} Positions={counts['broker_positions']} Fills={counts['broker_fills']}")
        print(f"  Ticket lifecycle={result['ticket_lifecycle']}")


if __name__ == "__main__":
    main()
