#!/usr/bin/env python3.12
"""Match execution tickets and journal trades to broker fills and positions."""
from __future__ import annotations

import argparse
import json
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any

from common import atomic_write_json, parse_osi_parts, state_lock


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
    effect = normalize(fill.get("execution_effect"))
    if effect and effect != "OPEN":
        return 0
    fill_ticket_id = normalize(fill.get("ticket_id"))
    ticket_id = normalize(ticket.get("ticket_id"))
    if fill_ticket_id:
        return 100 if ticket_id and ticket_id == fill_ticket_id else 0
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


def closing_fill_score(trade: dict[str, Any], fill: dict[str, Any]) -> int:
    if normalize(fill.get("execution_effect")) != "CLOSE":
        return 0
    score = 0
    if normalize(trade.get("ticker")) == fill_ticker(fill):
        score += 35
    if normalize(trade.get("strategy")) == normalize(fill.get("strategy")):
        score += 25
    if str(trade.get("expiration") or "") == str(fill.get("expiration") or ""):
        score += 20
    if normalize(trade.get("strikes")) == normalize(fill.get("strikes")):
        score += 20
    return score


def match_closing_fills(
    trades: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    minimum_score: int = 60,
) -> tuple[list[dict[str, Any]], set[int]]:
    available = {
        index
        for index, fill in enumerate(fills)
        if normalize(fill.get("execution_effect")) == "CLOSE"
    }
    matches = []
    matched_indices: set[int] = set()
    for trade in trades:
        if normalize(trade.get("status")) != "OPEN":
            continue
        ranked = sorted(
            (
                (closing_fill_score(trade, fills[index]), index)
                for index in available
            ),
            reverse=True,
        )
        score, index = ranked[0] if ranked else (0, -1)
        if score < minimum_score:
            continue
        fill = fills[index]
        available.remove(index)
        matched_indices.add(index)
        target_quantity = as_quantity(trade.get("quantity"), 1.0) or 1.0
        quantity = fill_quantity(fill)
        matches.append(
            {
                "trade_id": trade.get("id"),
                "ticket_id": trade.get("ticket_id"),
                "status": "CLOSE_MATCHED" if quantity >= target_quantity else "CLOSE_PARTIAL",
                "match_score": score,
                "fill_id": fill.get("fill_id") or fill.get("id"),
                "ticker": trade.get("ticker"),
                "strategy": trade.get("strategy"),
                "exit_price": abs(fill_price(fill)) if fill_price(fill) is not None else None,
                "quantity": quantity,
                "target_quantity": target_quantity,
                "fees": fill.get("fees"),
                "filled_at": fill.get("filled_at"),
                "classification_confidence": fill.get("classification_confidence"),
            }
        )
    return matches, matched_indices


def as_quantity(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return abs(parsed)


def fill_quantity(fill: dict[str, Any]) -> float:
    return as_quantity(fill.get("quantity"), 1.0) or 1.0


def fill_price(fill: dict[str, Any]) -> float | None:
    for key in ("net_credit", "price", "credit"):
        value = fill.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def aggregate_fill_match(
    ticket: dict[str, Any],
    selected: list[tuple[int, dict[str, Any], int]],
) -> dict[str, Any]:
    target = as_quantity(ticket.get("target_quantity"), 1.0) or 1.0
    filled = sum(fill_quantity(fill) for _, fill, _ in selected)
    priced_quantity = sum(
        fill_quantity(fill)
        for _, fill, _ in selected
        if fill_price(fill) is not None
    )
    weighted_price = (
        sum(fill_price(fill) * fill_quantity(fill) for _, fill, _ in selected if fill_price(fill) is not None)
        / priced_quantity
        if priced_quantity
        else None
    )
    if filled < target:
        status = "PARTIAL"
    elif filled > target:
        status = "OVERFILLED"
    else:
        status = "MATCHED"
    fill_ids = [fill.get("fill_id") or fill.get("id") for _, fill, _ in selected]
    filled_times = [
        str(fill.get("filled_at"))
        for _, fill, _ in selected
        if fill.get("filled_at")
    ]
    fill_delay_seconds = None
    if ticket.get("issued_at") and filled_times:
        try:
            issued = datetime.fromisoformat(str(ticket.get("issued_at")).replace("Z", "+00:00"))
            completed = datetime.fromisoformat(max(filled_times).replace("Z", "+00:00"))
            fill_delay_seconds = max(0.0, (completed - issued).total_seconds())
        except (TypeError, ValueError):
            pass
    return {
        "ticket_id": ticket.get("ticket_id"),
        "status": status,
        "match_score": min(score for _, _, score in selected),
        "fill_id": fill_ids[0] if fill_ids else None,
        "fill_ids": fill_ids,
        "fill_count": len(selected),
        "ticker": ticket.get("ticker"),
        "strategy": ticket.get("strategy"),
        "execution_grade": ticket.get("execution_grade"),
        "do_not_chase_below": ticket.get("do_not_chase_below"),
        "planned_limit_credit": ticket.get("limit_credit"),
        "fill_price": round(weighted_price, 4) if weighted_price is not None else None,
        "target_quantity": target,
        "filled_quantity": round(filled, 4),
        "remaining_quantity": round(max(0.0, target - filled), 4),
        "fees": round(sum(float(fill.get("fees") or 0) for _, fill, _ in selected), 4),
        "first_fill_at": min(filled_times) if filled_times else None,
        "last_fill_at": max(filled_times) if filled_times else None,
        "fill_delay_seconds": fill_delay_seconds,
    }


def match_tickets(
    tickets: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    minimum_score: int = 60,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    available = set(range(len(fills)))
    matches = []
    for ticket in tickets:
        target = as_quantity(ticket.get("target_quantity"), 1.0) or 1.0
        scored = ((ticket_fill_score(ticket, fills[idx]), idx) for idx in available)
        ranked = sorted(
            (
                (score, idx)
                for score, idx in scored
                if score >= minimum_score
            ),
            key=lambda item: (
                -item[0],
                str(fills[item[1]].get("filled_at") or ""),
                str(fills[item[1]].get("fill_id") or fills[item[1]].get("id") or ""),
            ),
        )
        exact = [
            (idx, fills[idx], score)
            for score, idx in ranked
            if score == 100
        ]
        selected = exact
        if not selected:
            selected = []
            selected_quantity = 0.0
            for score, idx in ranked:
                selected.append((idx, fills[idx], score))
                selected_quantity += fill_quantity(fills[idx])
                if selected_quantity >= target:
                    break
        if selected:
            for idx, _, _ in selected:
                available.remove(idx)
            matches.append(aggregate_fill_match(ticket, selected))
        else:
            matches.append(
                {
                    "ticket_id": ticket.get("ticket_id"),
                    "status": "UNMATCHED",
                    "match_score": 0,
                    "ticker": ticket.get("ticker"),
                    "strategy": ticket.get("strategy"),
                    "target_quantity": target,
                    "filled_quantity": 0.0,
                    "remaining_quantity": target,
                    "fill_ids": [],
                    "fill_count": 0,
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
        if match.get("status") not in {"MATCHED", "OVERFILLED"}:
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
                        "broker_fill_ids": match.get("fill_ids"),
                        "filled_quantity": match.get("filled_quantity"),
                    },
                    "apply_automatically": False,
                }
            )
    return updates


def proposed_exit_updates(
    journal: dict[str, Any],
    exit_matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    trades = {str(trade.get("id")): trade for trade in journal.get("trades", [])}
    updates = []
    for match in exit_matches:
        if match.get("status") != "CLOSE_MATCHED":
            continue
        trade = trades.get(str(match.get("trade_id"))) or {}
        quantity = as_quantity(trade.get("quantity"), 1.0) or 1.0
        exit_debit = float(match.get("exit_price") or 0)
        fees = float(match.get("fees") or 0)
        realized_pnl = None
        if trade.get("entry_credit") is not None:
            realized_pnl = round(
                (float(trade.get("entry_credit")) - exit_debit) * 100 * quantity - fees,
                2,
            )
        capital = float(trade.get("capital_at_risk") or trade.get("max_loss") or 0)
        updates.append(
            {
                "trade_id": match.get("trade_id"),
                "ticket_id": match.get("ticket_id"),
                "set": {
                    "status": "CLOSED",
                    "exit_debit": exit_debit,
                    "closed_at": match.get("filled_at"),
                    "broker_exit_fill_id": match.get("fill_id"),
                    "exit_fees": fees,
                    "realized_pnl": realized_pnl,
                    "realized_return_pct": (
                        round(realized_pnl / capital * 100, 2)
                        if realized_pnl is not None and capital
                        else None
                    ),
                },
                "apply_automatically": False,
                "classification_confidence": match.get("classification_confidence"),
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
    exit_matches, exit_fill_indices = match_closing_fills(journal.get("trades", []), fills)
    exit_fill_ids = {
        fills[index].get("fill_id") or fills[index].get("id")
        for index in exit_fill_indices
    }
    unmatched_fills = [
        fill
        for fill in unmatched_fills
        if (fill.get("fill_id") or fill.get("id")) not in exit_fill_ids
    ]
    trade_positions = reconcile_open_trades(journal.get("trades", []), positions)
    return {
        "created_at": datetime.now().isoformat(),
        "summary": {
            "tickets": len(tickets),
            "matched_tickets": sum(1 for row in ticket_matches if row["status"] in {"MATCHED", "OVERFILLED"}),
            "partial_tickets": sum(1 for row in ticket_matches if row["status"] == "PARTIAL"),
            "overfilled_tickets": sum(1 for row in ticket_matches if row["status"] == "OVERFILLED"),
            "unmatched_tickets": sum(1 for row in ticket_matches if row["status"] == "UNMATCHED"),
            "target_quantity": round(sum(as_quantity(row.get("target_quantity")) for row in ticket_matches), 4),
            "filled_quantity": round(sum(as_quantity(row.get("filled_quantity")) for row in ticket_matches), 4),
            "unmatched_fills": len(unmatched_fills),
            "matched_exit_fills": len(exit_matches),
            "unknown_effect_fills": sum(
                1 for fill in fills if normalize(fill.get("execution_effect")) == "UNKNOWN"
            ),
            "open_trades": len(trade_positions),
            "missing_positions": sum(1 for row in trade_positions if row["status"] == "MISSING_POSITION"),
            "partial_positions": sum(1 for row in trade_positions if row["status"] == "PARTIAL_POSITION"),
            "position_exceptions": sum(1 for row in trade_positions if row["status"] != "POSITION_FOUND"),
        },
        "ticket_matches": ticket_matches,
        "trade_exit_matches": exit_matches,
        "unmatched_fills": unmatched_fills,
        "trade_positions": trade_positions,
        "proposed_journal_updates": proposed_journal_updates(journal, ticket_matches),
        "proposed_exit_updates": proposed_exit_updates(journal, exit_matches),
        "lifecycle_events": broker_snapshot.get("lifecycle_events", []),
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
    # Hold the journal lock across read-reconcile-write when applying so a
    # concurrent journal add/close or mark-to-market run cannot interleave.
    with state_lock("journal") if args.apply_updates else nullcontext():
        journal = json.loads(journal_path.read_text())
        report = build_reconciliation(
            journal,
            json.loads(Path(args.tickets).read_text()),
            json.loads(Path(args.broker_snapshot).read_text()),
        )
        if args.apply_updates:
            applied = apply_journal_updates(
                journal,
                [
                    *report.get("proposed_journal_updates", []),
                    *report.get("proposed_exit_updates", []),
                ],
            )
            output_path = Path(args.journal_output) if args.journal_output else journal_path
            atomic_write_json(output_path, applied["journal"])
            if args.db:
                from storage import connect, upsert_trades

                con = connect(args.db)
                try:
                    upsert_trades(con, applied["journal"].get("trades", []))
                finally:
                    con.close()
            report["applied_journal_updates"] = applied["applied_updates"]
            report["applied_exit_updates"] = [
                update
                for update in applied["applied_updates"]
                if update.get("set", {}).get("status") == "CLOSED"
            ]
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
