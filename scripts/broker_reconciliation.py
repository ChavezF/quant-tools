#!/usr/bin/env python3.12
"""Match execution tickets and journal trades to broker fills and positions."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from common import parse_osi_parts


def normalize(value: Any) -> str:
    return str(value or "").strip().upper()


def position_underlying(position: dict[str, Any]) -> str:
    symbol = normalize(position.get("symbol") or position.get("ticker"))
    if normalize(position.get("type")) == "OPTION":
        return normalize(parse_osi_parts(symbol).get("underlying"))
    parsed = parse_osi_parts(symbol)
    return normalize(parsed.get("underlying")) if parsed.get("option_type") else symbol


def fill_ticker(fill: dict[str, Any]) -> str:
    ticker = normalize(fill.get("ticker"))
    if ticker:
        return ticker
    symbol = normalize(fill.get("symbol"))
    return normalize(parse_osi_parts(symbol).get("underlying")) if symbol else ""


def ticket_fill_score(ticket: dict[str, Any], fill: dict[str, Any]) -> int:
    if ticket.get("ticket_id") and normalize(ticket.get("ticket_id")) == normalize(fill.get("ticket_id")):
        return 100
    score = 0
    if normalize(ticket.get("ticker")) and normalize(ticket.get("ticker")) == fill_ticker(fill):
        score += 35
    if normalize(ticket.get("strategy")) and normalize(ticket.get("strategy")) == normalize(fill.get("strategy")):
        score += 25
    if str(ticket.get("expiration") or "") and str(ticket.get("expiration")) == str(fill.get("expiration") or ""):
        score += 20
    if normalize(ticket.get("strikes")) and normalize(ticket.get("strikes")) == normalize(fill.get("strikes")):
        score += 20
    return score


def match_tickets(
    tickets: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    minimum_score: int = 60,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    available = set(range(len(fills)))
    matches = []
    for ticket in tickets:
        ranked = sorted(
            ((ticket_fill_score(ticket, fills[idx]), idx) for idx in available),
            reverse=True,
        )
        score, match_idx = ranked[0] if ranked else (0, -1)
        if score >= minimum_score:
            fill = fills[match_idx]
            available.remove(match_idx)
            matches.append(
                {
                    "ticket_id": ticket.get("ticket_id"),
                    "status": "MATCHED",
                    "match_score": score,
                    "fill_id": fill.get("fill_id") or fill.get("id"),
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                    "planned_limit_credit": ticket.get("limit_credit"),
                    "fill_price": fill.get("net_credit") or fill.get("price") or fill.get("credit"),
                }
            )
        else:
            matches.append(
                {
                    "ticket_id": ticket.get("ticket_id"),
                    "status": "UNMATCHED",
                    "match_score": score,
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                }
            )
    return matches, [fills[idx] for idx in sorted(available)]


def reconcile_open_trades(
    trades: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    position_map: dict[str, list[dict[str, Any]]] = {}
    for position in positions:
        position_map.setdefault(position_underlying(position), []).append(position)

    rows = []
    for trade in trades:
        if normalize(trade.get("status")) != "OPEN":
            continue
        ticker = normalize(trade.get("ticker"))
        candidates = position_map.get(ticker, [])
        expiration = str(trade.get("expiration") or "")
        expected_strikes = {
            float(value)
            for value in str(trade.get("strikes") or "").split("/")
            if value and value.replace(".", "", 1).isdigit()
        }
        matched = []
        matched_strikes = set()
        for position in candidates:
            parsed = parse_osi_parts(normalize(position.get("symbol")))
            if expiration and parsed.get("option_type") and parsed.get("expiration") != expiration:
                continue
            strike = parsed.get("strike")
            if expected_strikes and strike is not None and float(strike) not in expected_strikes:
                continue
            matched.append(position)
            if strike is not None:
                matched_strikes.add(float(strike))

        if not matched:
            status = "MISSING_POSITION"
        elif expected_strikes and not expected_strikes.issubset(matched_strikes):
            status = "PARTIAL_POSITION"
        else:
            status = "POSITION_FOUND"
        rows.append(
            {
                "trade_id": trade.get("id"),
                "ticket_id": trade.get("ticket_id"),
                "ticker": ticker,
                "strategy": trade.get("strategy"),
                "status": status,
                "position_symbols": [position.get("symbol") for position in matched],
                "net_quantity": round(sum(float(position.get("quantity") or 0) for position in matched), 4),
                "expected_strikes": sorted(expected_strikes),
                "matched_strikes": sorted(matched_strikes),
            }
        )
    return rows


def proposed_journal_updates(
    journal: dict[str, Any],
    ticket_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trades_by_ticket = {
        normalize(trade.get("ticket_id")): trade
        for trade in journal.get("trades", [])
        if trade.get("ticket_id")
    }
    updates = []
    for match in ticket_matches:
        if match.get("status") != "MATCHED":
            continue
        trade = trades_by_ticket.get(normalize(match.get("ticket_id")))
        if trade:
            updates.append(
                {
                    "trade_id": trade.get("id"),
                    "ticket_id": match.get("ticket_id"),
                    "set": {
                        "planned_limit_credit": match.get("planned_limit_credit"),
                        "entry_credit": match.get("fill_price"),
                        "broker_fill_id": match.get("fill_id"),
                    },
                    "apply_automatically": False,
                }
            )
    return updates


def apply_journal_updates(journal: dict[str, Any], updates: list[dict[str, Any]]) -> dict[str, Any]:
    trades_by_id = {str(trade.get("id")): trade for trade in journal.get("trades", [])}
    applied = []
    for update in updates:
        trade = trades_by_id.get(str(update.get("trade_id")))
        if not trade:
            continue
        changed = {}
        for key, value in update.get("set", {}).items():
            if value is not None and trade.get(key) != value:
                trade[key] = value
                changed[key] = value
        if changed:
            applied.append({"trade_id": update.get("trade_id"), "ticket_id": update.get("ticket_id"), "set": changed})
    return {"journal": journal, "applied_updates": applied}


def build_reconciliation(
    journal: dict[str, Any],
    tickets_report: dict[str, Any],
    broker_snapshot: dict[str, Any],
) -> dict[str, Any]:
    tickets = tickets_report.get("tickets", [])
    fills = broker_snapshot.get("fills", [])
    positions = broker_snapshot.get("positions", [])
    ticket_matches, unmatched_fills = match_tickets(tickets, fills)
    trade_positions = reconcile_open_trades(journal.get("trades", []), positions)
    return {
        "created_at": datetime.now().isoformat(),
        "summary": {
            "tickets": len(tickets),
            "matched_tickets": sum(1 for row in ticket_matches if row["status"] == "MATCHED"),
            "unmatched_tickets": sum(1 for row in ticket_matches if row["status"] == "UNMATCHED"),
            "unmatched_fills": len(unmatched_fills),
            "open_trades": len(trade_positions),
            "missing_positions": sum(1 for row in trade_positions if row["status"] == "MISSING_POSITION"),
            "partial_positions": sum(1 for row in trade_positions if row["status"] == "PARTIAL_POSITION"),
            "position_exceptions": sum(1 for row in trade_positions if row["status"] != "POSITION_FOUND"),
        },
        "ticket_matches": ticket_matches,
        "unmatched_fills": unmatched_fills,
        "trade_positions": trade_positions,
        "proposed_journal_updates": proposed_journal_updates(journal, ticket_matches),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", required=True)
    ap.add_argument("--tickets", required=True)
    ap.add_argument("--broker-snapshot", required=True)
    ap.add_argument("--output")
    ap.add_argument("--apply-updates", action="store_true", help="Write proposed fill updates into the journal")
    ap.add_argument("--journal-output", help="Write updated journal here; defaults to --journal when applying")
    ap.add_argument("--db", help="Optional SQLite database to update with the applied journal")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    journal_path = Path(args.journal)
    journal = json.loads(journal_path.read_text())
    report = build_reconciliation(
        journal,
        json.loads(Path(args.tickets).read_text()),
        json.loads(Path(args.broker_snapshot).read_text()),
    )
    if args.apply_updates:
        applied = apply_journal_updates(journal, report.get("proposed_journal_updates", []))
        output_path = Path(args.journal_output) if args.journal_output else journal_path
        output_path.write_text(json.dumps(applied["journal"], indent=2, default=str))
        if args.db:
            from storage import connect, upsert_trades

            con = connect(args.db)
            try:
                upsert_trades(con, applied["journal"].get("trades", []))
            finally:
                con.close()
        report["applied_journal_updates"] = applied["applied_updates"]
        report["summary"]["applied_journal_updates"] = len(applied["applied_updates"])
        report["journal_output"] = str(output_path)
        report["database_updated"] = bool(args.db)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        summary = report["summary"]
        print(
            f"Matched tickets: {summary['matched_tickets']}/{summary['tickets']} | "
            f"Missing positions: {summary['missing_positions']}/{summary['open_trades']}"
        )


if __name__ == "__main__":
    main()
