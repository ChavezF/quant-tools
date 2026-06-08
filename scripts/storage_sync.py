#!/usr/bin/env python3.12
"""Sync JSON workflow artifacts into the SQLite persistence store."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from broker_reconciliation import build_reconciliation
from storage import DEFAULT_DB_FILE, connect, export_journal_state, insert_position_snapshot, record_reconciliation, table_counts, upsert_fills, upsert_tickets, upsert_trades
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(DEFAULT_DB_FILE))
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--tickets")
    ap.add_argument("--portfolio")
    ap.add_argument("--broker-snapshot")
    ap.add_argument("--output")
    ap.add_argument("--export-journal")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    journal = load_state(Path(args.journal))
    tickets = read_json(args.tickets).get("tickets", [])
    portfolio = read_json(args.portfolio)
    broker_snapshot = read_json(args.broker_snapshot)
    positions = positions_from_portfolio(portfolio) or broker_snapshot.get("positions", [])
    fills = broker_snapshot.get("fills", [])
    report = build_reconciliation(journal, {"tickets": tickets}, {"positions": positions, "fills": fills})

    con = connect(args.db)
    try:
        imported = {
            "trades": upsert_trades(con, journal.get("trades", [])),
            "tickets": upsert_tickets(con, tickets),
            "positions": insert_position_snapshot(con, positions) if positions else 0,
            "fills": upsert_fills(con, fills),
            "reconciliation_run_id": record_reconciliation(con, report),
        }
        counts = table_counts(con)
        if args.export_journal:
            Path(args.export_journal).write_text(json.dumps(export_journal_state(con), indent=2, default=str))
    finally:
        con.close()

    result = {"imported": imported, "counts": counts, "reconciliation": report}
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2, default=str))
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"SQLite sync complete: {args.db}")
        print(f"  Trades={counts['trades']} Tickets={counts['tickets']} Positions={counts['broker_positions']} Fills={counts['broker_fills']}")


if __name__ == "__main__":
    main()
