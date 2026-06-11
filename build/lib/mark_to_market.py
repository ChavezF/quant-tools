#!/usr/bin/env python3.12
"""Mark open journal trades to market so profit-target alerts can fire.

For every OPEN trade in the journal, rebuild its option legs from the stored
strategy/expiration/strikes, price the current cost to close from the live
Public.com chain, and stamp the trade with:

  unrealized_pnl       dollars, same sign convention as realized_pnl
  unrealized_pnl_pct   percent of MAX PROFIT captured (the management number:
                       100 means the position could be closed for zero debit;
                       50 means half the entry credit has decayed away). This
                       matches `alerts --profit-target-pct` ("close at 50%")
                       and is deliberately NOT measured against capital at
                       risk like realized_pnl_pct.
  mark_cost_to_close   current per-share debit to exit the structure
  marked_at            ISO timestamp of the mark

Only net-credit structures are marked (this toolkit sells premium). Trades
whose legs cannot be rebuilt or priced are reported as SKIPPED/UNMARKED rather
than guessed at.

Usage:
  ./mark_to_market.py --journal state/trades.json
  ./mark_to_market.py --journal state/trades.json --db state/quant_tools.db --json
  ./mark_to_market.py --dry-run        # compute and report, do not save
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from common import parse_osi_parts, state_lock
from data_reliability import retry_call
from trade_journal import DEFAULT_STATE_FILE, load_backend, save_backend


# (ticker, expiration, option_type "C"/"P", strike) -> per-share mark or None
MarkLookup = Callable[[str, str, str, float], float | None]

SHORT = "SHORT"
LONG = "LONG"


def parse_strikes(raw: Any) -> list[float]:
    """Parse the journal's strikes field ("475", "470/475", "440/445/505/510")."""
    strikes = []
    for part in str(raw or "").replace(",", "/").split("/"):
        part = part.strip()
        if not part:
            continue
        try:
            strikes.append(float(part))
        except ValueError:
            return []
    return strikes


def leg(side: str, option_type: str, strike: float) -> dict[str, Any]:
    return {"side": side, "option_type": option_type, "strike": strike}


def trade_legs(trade: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Rebuild option legs from the journal row. Returns (legs, error_reason)."""
    strategy = str(trade.get("strategy") or "").upper().replace("-", "_").replace(" ", "_")
    strikes = sorted(parse_strikes(trade.get("strikes")))
    if not str(trade.get("expiration") or ""):
        return None, "missing expiration"

    def need(count: int) -> str | None:
        return None if len(strikes) == count else f"expected {count} strike(s), have {len(strikes)}"

    if strategy in {"CSP", "SHORT_PUT", "CASH_SECURED_PUT"}:
        return ([leg(SHORT, "P", strikes[0])], None) if not need(1) else (None, need(1))
    if strategy in {"CC", "COVERED_CALL", "SHORT_CALL"}:
        return ([leg(SHORT, "C", strikes[0])], None) if not need(1) else (None, need(1))
    if strategy in {"BULL_PUT", "BULL_PUT_SPREAD", "PUT_CREDIT_SPREAD"}:
        if need(2):
            return None, need(2)
        return [leg(SHORT, "P", strikes[1]), leg(LONG, "P", strikes[0])], None
    if strategy in {"BEAR_CALL", "BEAR_CALL_SPREAD", "CALL_CREDIT_SPREAD"}:
        if need(2):
            return None, need(2)
        return [leg(SHORT, "C", strikes[0]), leg(LONG, "C", strikes[1])], None
    if strategy in {"SHORT_STRANGLE", "STRANGLE"}:
        if need(2):
            return None, need(2)
        return [leg(SHORT, "P", strikes[0]), leg(SHORT, "C", strikes[1])], None
    if strategy in {"IRON_CONDOR", "IC"}:
        if need(4):
            return None, need(4)
        return [
            leg(LONG, "P", strikes[0]),
            leg(SHORT, "P", strikes[1]),
            leg(SHORT, "C", strikes[2]),
            leg(LONG, "C", strikes[3]),
        ], None
    return None, f"unsupported strategy: {strategy or 'EMPTY'}"


def mark_trade(trade: dict[str, Any], mark_lookup: MarkLookup, now_iso: str) -> dict[str, Any]:
    """Price one open trade and stamp unrealized fields onto it.

    Returns a report row; mutates `trade` only when fully priced (MARKED).
    """
    row = {
        "trade_id": trade.get("id"),
        "ticket_id": trade.get("ticket_id"),
        "ticker": trade.get("ticker"),
        "strategy": trade.get("strategy"),
    }
    legs, reason = trade_legs(trade)
    if legs is None:
        return {**row, "status": "SKIPPED", "reason": reason}
    net_entry = float(trade.get("entry_credit") or 0) - float(trade.get("entry_debit") or 0)
    if net_entry <= 0:
        return {**row, "status": "SKIPPED", "reason": "not a net-credit position"}

    ticker = str(trade.get("ticker") or "").upper()
    expiration = str(trade.get("expiration"))
    cost_to_close = 0.0
    for piece in legs:
        mark = mark_lookup(ticker, expiration, piece["option_type"], piece["strike"])
        if mark is None:
            return {
                **row,
                "status": "UNMARKED",
                "reason": f"no mark for {ticker} {expiration} {piece['option_type']}{piece['strike']:g}",
            }
        cost_to_close += mark if piece["side"] == SHORT else -mark

    quantity = float(trade.get("quantity") or 1) or 1
    unrealized_pnl = (net_entry - cost_to_close) * quantity * 100
    unrealized_pnl_pct = (net_entry - cost_to_close) / net_entry * 100
    trade["unrealized_pnl"] = round(unrealized_pnl, 2)
    trade["unrealized_pnl_pct"] = round(unrealized_pnl_pct, 2)
    trade["mark_cost_to_close"] = round(cost_to_close, 4)
    trade["marked_at"] = now_iso
    return {
        **row,
        "status": "MARKED",
        "net_entry": round(net_entry, 4),
        "cost_to_close": round(cost_to_close, 4),
        "unrealized_pnl": trade["unrealized_pnl"],
        "unrealized_pnl_pct": trade["unrealized_pnl_pct"],
    }


def mark_open_trades(
    state: dict[str, Any],
    mark_lookup: MarkLookup,
    now_iso: str | None = None,
) -> dict[str, Any]:
    now_iso = now_iso or datetime.now().isoformat()
    rows = [
        mark_trade(trade, mark_lookup, now_iso)
        for trade in state.get("trades", [])
        if trade.get("status") == "OPEN"
    ]
    return {
        "as_of": now_iso,
        "summary": {
            "open_trades": len(rows),
            "marked": sum(1 for row in rows if row["status"] == "MARKED"),
            "unmarked": sum(1 for row in rows if row["status"] == "UNMARKED"),
            "skipped": sum(1 for row in rows if row["status"] == "SKIPPED"),
        },
        "marks": rows,
    }


class ChainMarkSource:
    """Live MarkLookup backed by Public.com chains, one fetch per (ticker, expiration)."""

    def __init__(self, client, retries: int = 2):
        self.client = client
        self.retries = retries
        self._chains: dict[tuple[str, str], dict[tuple[str, float], float]] = {}

    def _fetch(self, ticker: str, expiration: str) -> dict[tuple[str, float], float]:
        from public_api_sdk import InstrumentType, OptionChainRequest, OrderInstrument

        def _call():
            return self.client.get_option_chain(OptionChainRequest(
                instrument=OrderInstrument(symbol=ticker, type=InstrumentType.EQUITY),
                expiration_date=expiration,
            ))

        chain, meta = retry_call(_call, source=f"public.chain.{ticker}.{expiration}", retries=self.retries)
        if not meta.ok or chain is None:
            print(f"  ! chain failed for {ticker} {expiration}: {meta.error}", file=sys.stderr)
            return {}
        marks: dict[tuple[str, float], float] = {}
        for option in (chain.calls or []) + (chain.puts or []):
            instrument = getattr(option, "instrument", None)
            osi = getattr(instrument, "symbol", None) if instrument else None
            if not osi:
                continue
            parts = parse_osi_parts(osi)
            strike = parts.get("strike")
            if strike is None:
                continue
            bid = float(option.bid) if getattr(option, "bid", None) else 0.0
            ask = float(option.ask) if getattr(option, "ask", None) else 0.0
            if bid and ask:
                mark = (bid + ask) / 2
            elif getattr(option, "last", None):
                mark = float(option.last)
            else:
                continue
            marks[(str(parts["option_type"]), round(float(strike), 3))] = mark
        return marks

    def __call__(self, ticker: str, expiration: str, option_type: str, strike: float) -> float | None:
        key = (ticker, expiration)
        if key not in self._chains:
            self._chains[key] = self._fetch(ticker, expiration)
        return self._chains[key].get((option_type, round(float(strike), 3)))


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#'*78}")
    print("# MARK TO MARKET — open journal trades")
    print(f"{'#'*78}\n")
    print(
        f"  Open={summary['open_trades']}  Marked={summary['marked']}  "
        f"Unmarked={summary['unmarked']}  Skipped={summary['skipped']}"
    )
    for row in report["marks"]:
        if row["status"] == "MARKED":
            print(
                f"  {row['trade_id']:<14} {row['ticker']:<6} {row['strategy']:<12} "
                f"close@{row['cost_to_close']:.2f}  P&L ${row['unrealized_pnl']:>9,.2f} "
                f"({row['unrealized_pnl_pct']:+.1f}% of max profit)"
            )
        else:
            print(f"  {row['trade_id']:<14} {row['status']}: {row.get('reason')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; JSON remains dual-written")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="Compute marks without saving the journal")
    args = ap.parse_args()

    state_file = Path(args.journal)
    with nullcontext() if args.dry_run else state_lock("journal"):
        state, con = load_backend(state_file, args.db)
        has_open = any(trade.get("status") == "OPEN" for trade in state.get("trades", []))
        if has_open:
            from common import get_public_client

            report = mark_open_trades(state, ChainMarkSource(get_public_client()))
        else:
            report = mark_open_trades(state, lambda *parts: None)
        if not args.dry_run and report["summary"]["marked"]:
            save_backend(state_file, state, con)
        if con is not None:
            con.close()

    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
