#!/usr/bin/env python3.12
"""Turn a READY ticket into a manual order packet and optional staged journal row."""
from __future__ import annotations

import argparse
import json
import os
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from common import format_osi_symbol, state_lock
from storage import DEFAULT_DB_FILE, connect, get_ticket
from trade_journal import DEFAULT_STATE_FILE, load_backend, next_trade_id, save_backend


DEFAULT_ORDER_HELPER = (
    "/home/chavez_f/.hermes/skills/openclaw-imports/"
    "public-dot-com/scripts/place_order.py"
)


def parse_strikes(ticket: dict[str, Any]) -> list[float]:
    values = []
    for raw in str(ticket.get("strikes") or "").split("/"):
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def order_legs(ticket: dict[str, Any]) -> list[dict[str, Any]]:
    ticker = str(ticket.get("ticker") or "").upper()
    expiration = str(ticket.get("expiration") or "")
    strategy = str(ticket.get("strategy") or "").upper()
    strikes = parse_strikes(ticket)
    if strategy == "CSP" and len(strikes) == 1:
        return [{"symbol": format_osi_symbol(ticker, expiration, "P", strikes[0]), "side": "SELL", "effect": "OPEN"}]
    if strategy == "CC" and len(strikes) == 1:
        return [{"symbol": format_osi_symbol(ticker, expiration, "C", strikes[0]), "side": "SELL", "effect": "OPEN"}]
    if strategy == "BULL_PUT" and len(strikes) == 2:
        return [
            {"symbol": format_osi_symbol(ticker, expiration, "P", strikes[0]), "side": "SELL", "effect": "OPEN"},
            {"symbol": format_osi_symbol(ticker, expiration, "P", strikes[1]), "side": "BUY", "effect": "OPEN"},
        ]
    raise ValueError(f"Unsupported or incomplete staged strategy: {strategy} {ticket.get('strikes')}")


def helper_command(ticket: dict[str, Any], legs: list[dict[str, Any]], helper: str) -> list[str] | None:
    if len(legs) != 1:
        return None
    leg = legs[0]
    return [
        "/usr/bin/python3.12",
        helper,
        "--symbol",
        leg["symbol"],
        "--type",
        "OPTION",
        "--side",
        leg["side"],
        "--order-type",
        "LIMIT",
        "--quantity",
        str(ticket.get("target_quantity") or 1),
        "--limit-price",
        str(ticket.get("limit_credit")),
        "--open-close",
        "OPEN",
        "--time-in-force",
        "DAY",
    ]


def build_stage_packet(ticket: dict[str, Any], helper: str = DEFAULT_ORDER_HELPER) -> dict[str, Any]:
    status = str(ticket.get("lifecycle_status") or "").upper()
    if status != "READY":
        raise ValueError(f"Ticket must be READY before staging; current status is {status or 'UNKNOWN'}")
    try:
        limit_credit = float(ticket.get("limit_credit"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Ticket must include a positive limit_credit before staging") from exc
    if limit_credit <= 0:
        raise ValueError("Ticket must include a positive limit_credit before staging")
    legs = order_legs(ticket)
    command = helper_command(ticket, legs, helper)
    return {
        "ticket_id": ticket["ticket_id"],
        "stage_status": "READY_FOR_MANUAL_SUBMISSION" if command else "BROKER_HELPER_UNSUPPORTED",
        "manual_order": {
            "strategy": ticket.get("strategy"),
            "order_type": "NET_CREDIT_LIMIT",
            "limit_credit": limit_credit,
            "do_not_chase_below": ticket.get("do_not_chase_below"),
            "quantity": ticket.get("target_quantity") or 1,
            "time_in_force": "DAY",
            "legs": legs,
        },
        "place_order_command": shlex.join(command) if command else None,
        "warning": (
            None
            if command
            else "Installed place_order.py is single-leg only. Submit this spread as one multi-leg order; do not leg in."
        ),
        "safety": "This command does not place an order. Broker submission still requires explicit operator action.",
    }


def journal_trade_from_ticket(ticket: dict[str, Any], trade_id: str) -> dict[str, Any]:
    return {
        "id": trade_id,
        "ticket_id": ticket.get("ticket_id"),
        "status": "STAGED",
        "ticker": str(ticket.get("ticker") or "").upper(),
        "strategy": str(ticket.get("strategy") or "").upper(),
        "staged_at": datetime.now().isoformat(),
        "opened_at": None,
        "closed_at": None,
        "quantity": float(ticket.get("target_quantity") or 1),
        "entry_credit": None,
        "planned_limit_credit": ticket.get("limit_credit"),
        "entry_debit": 0.0,
        "exit_credit": None,
        "exit_debit": None,
        "capital_at_risk": float(ticket.get("capital_required") or ticket.get("max_loss") or 0),
        "max_loss": float(ticket.get("max_loss") or 0),
        "score": ticket.get("score"),
        "verdict": ticket.get("verdict") or ticket.get("decision"),
        "pop_pct": ticket.get("pop_pct"),
        "ann_roc_pct": ticket.get("ann_roc_pct"),
        "dte": ticket.get("dte"),
        "expiration": ticket.get("expiration"),
        "strikes": ticket.get("strikes"),
        "thesis": f"Staged from execution ticket {ticket.get('ticket_id')}",
        "tags": ["staged", "ticket"],
        "notes": [],
        "realized_pnl": None,
        "realized_pnl_pct": None,
    }


def confirm_journal_stage(
    ticket: dict[str, Any],
    state_file: Path,
    db_path: str | None,
) -> tuple[dict[str, Any], bool]:
    with state_lock("journal"):
        state, con = load_backend(state_file, db_path)
        try:
            existing = next(
                (
                    trade for trade in state.get("trades", [])
                    if str(trade.get("ticket_id") or "") == str(ticket.get("ticket_id"))
                ),
                None,
            )
            if existing:
                return existing, False
            trade = journal_trade_from_ticket(ticket, next_trade_id(state.setdefault("trades", [])))
            state["trades"].append(trade)
            save_backend(state_file, state, con)
            return trade, True
        finally:
            if con is not None:
                con.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB_FILE))
    parser.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--order-helper", default=os.environ.get("PUBLIC_ORDER_HELPER", DEFAULT_ORDER_HELPER))
    parser.add_argument("--confirm", action="store_true", help="Create the idempotent STAGED journal row")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    con = connect(args.db)
    try:
        ticket = get_ticket(con, args.ticket_id)
    finally:
        con.close()
    if not ticket:
        raise SystemExit(f"Unknown ticket_id: {args.ticket_id}")

    try:
        result = build_stage_packet(ticket, args.order_helper)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if args.confirm:
        trade, created = confirm_journal_stage(ticket, Path(args.journal), args.db)
        result["journal"] = {"created": created, "trade": trade}

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(f"Ticket {result['ticket_id']}: {result['stage_status']}")
    if result["place_order_command"]:
        print(result["place_order_command"])
    else:
        print(json.dumps(result["manual_order"], indent=2))
        print(result["warning"])
    if args.confirm:
        action = "Created" if result["journal"]["created"] else "Reused"
        print(f"{action} staged journal trade {result['journal']['trade']['id']}")


if __name__ == "__main__":
    main()
