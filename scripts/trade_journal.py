#!/usr/bin/env python3.12
"""
trade_journal.py - local trade journal and realized-performance tracker.

This is intentionally file-backed JSON to match the toolkit's current state
style. It can later be migrated to SQLite without changing the command surface.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from common import STATE_DIR, atomic_write_json, state_lock
from trade_stats import pnl_breakdown, profit_factor, trade_pnl


DEFAULT_STATE_FILE = STATE_DIR / "trades.json"


def default_state() -> dict[str, Any]:
    return {"version": 1, "last_updated": None, "trades": []}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return default_state()
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        # Never fall back to an empty journal here: every downstream report
        # (analytics, calibration, drift) would silently compute from nothing
        # and the next save would overwrite the only copy of the history.
        raise SystemExit(
            f"Trade journal {path} is corrupt ({exc}). Refusing to continue. "
            "Restore it from state/backups (db-maintenance keeps SQLite "
            "backups; `storage --export-journal` can rebuild the JSON) "
            "before rerunning."
        ) from exc
    state.setdefault("version", 1)
    state.setdefault("trades", [])
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["last_updated"] = datetime.now().isoformat()
    atomic_write_json(path, state)


def load_backend(state_file: Path, db_path: str | None) -> tuple[dict[str, Any], Any]:
    json_state = load_state(state_file)
    if not db_path:
        return json_state, None
    from storage import connect, export_journal_state, upsert_trades

    con = connect(db_path)
    db_state = export_journal_state(con)
    if db_state.get("trades"):
        return db_state, con
    upsert_trades(con, json_state.get("trades", []))
    return json_state, con


def save_backend(state_file: Path, state: dict[str, Any], con: Any = None) -> None:
    save_state(state_file, state)
    if con is not None:
        from storage import upsert_trades

        upsert_trades(con, state.get("trades", []))


def next_trade_id(trades: list[dict[str, Any]]) -> str:
    today = date.today().strftime("%Y%m%d")
    prefix = f"T{today}-"
    existing = [
        int(t["id"].split("-")[-1])
        for t in trades
        if str(t.get("id", "")).startswith(prefix) and str(t.get("id", "")).split("-")[-1].isdigit()
    ]
    return f"{prefix}{(max(existing) if existing else 0) + 1:03d}"


def tags_from_text(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [tag.strip() for tag in raw.split(",") if tag.strip()]


def add_trade(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    trades = state.setdefault("trades", [])
    trade = {
        "id": args.id or next_trade_id(trades),
        "ticket_id": getattr(args, "ticket_id", None),
        "status": "OPEN",
        "ticker": args.ticker.upper(),
        "strategy": args.strategy.upper(),
        "opened_at": args.opened_at or date.today().isoformat(),
        "closed_at": None,
        "quantity": args.quantity,
        "entry_credit": args.entry_credit,
        "planned_limit_credit": getattr(args, "planned_limit_credit", None),
        "entry_debit": args.entry_debit,
        "exit_credit": None,
        "exit_debit": None,
        "capital_at_risk": args.capital_at_risk,
        "max_loss": args.max_loss,
        "score": args.score,
        "verdict": args.verdict,
        "pop_pct": args.pop_pct,
        "ann_roc_pct": args.ann_roc_pct,
        "dte": args.dte,
        "expiration": args.expiration,
        "strikes": args.strikes,
        "thesis": args.thesis or "",
        "tags": tags_from_text(args.tags),
        "notes": [],
        "realized_pnl": None,
        "realized_pnl_pct": None,
    }
    trades.append(trade)
    return trade


def find_trade(state: dict[str, Any], trade_id: str) -> dict[str, Any]:
    for trade in state.get("trades", []):
        if trade.get("id") == trade_id:
            return trade
    raise SystemExit(f"Trade not found: {trade_id}")


def close_trade(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    trade = find_trade(state, args.id)
    if trade.get("status") == "CLOSED":
        raise SystemExit(f"Trade already closed: {args.id}")

    trade["status"] = "CLOSED"
    trade["closed_at"] = args.closed_at or date.today().isoformat()
    trade["exit_credit"] = args.exit_credit
    trade["exit_debit"] = args.exit_debit
    if args.note:
        trade.setdefault("notes", []).append({"at": datetime.now().isoformat(), "text": args.note})

    qty = float(trade.get("quantity", 1) or 1)
    entry_credit = float(trade.get("entry_credit") or 0)
    entry_debit = float(trade.get("entry_debit") or 0)
    exit_credit = float(args.exit_credit or 0)
    exit_debit = float(args.exit_debit or 0)
    pnl = (entry_credit - entry_debit + exit_credit - exit_debit) * qty * 100
    capital = float(trade.get("capital_at_risk") or trade.get("max_loss") or 0)
    trade["realized_pnl"] = round(pnl, 2)
    trade["realized_pnl_pct"] = round((pnl / capital * 100) if capital else 0, 2)
    return trade


def filter_trades(trades: list[dict[str, Any]], status: str | None = None) -> list[dict[str, Any]]:
    if not status or status.upper() == "ALL":
        return trades
    return [trade for trade in trades if str(trade.get("status", "")).upper() == status.upper()]


def journal_stats(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("status") == "CLOSED"]
    breakdown = pnl_breakdown(closed)

    by_strategy: dict[str, dict[str, Any]] = {}
    for trade in closed:
        strategy = str(trade.get("strategy") or "UNKNOWN")
        bucket = by_strategy.setdefault(strategy, {"count": 0, "pnl": 0.0, "wins": 0})
        pnl = trade_pnl(trade)
        bucket["count"] += 1
        bucket["pnl"] += pnl
        bucket["wins"] += 1 if pnl > 0 else 0

    for bucket in by_strategy.values():
        bucket["pnl"] = round(bucket["pnl"], 2)
        bucket["win_rate"] = round(bucket["wins"] / bucket["count"] * 100, 1) if bucket["count"] else 0

    return {
        "open_trades": sum(1 for trade in trades if trade.get("status") == "OPEN"),
        "closed_trades": breakdown["count"],
        "total_realized_pnl": round(breakdown["total_pnl"], 2),
        "win_rate": round(breakdown["wins"] / breakdown["count"] * 100, 1) if closed else 0,
        "avg_pnl": round(breakdown["total_pnl"] / breakdown["count"], 2) if closed else 0,
        "profit_factor": profit_factor(breakdown["gross_wins"], breakdown["gross_losses"]),
        "by_strategy": by_strategy,
    }


def print_trade_table(trades: list[dict[str, Any]]) -> None:
    print(f"  {'ID':<14} {'Status':<6} {'Ticker':<6} {'Strategy':<10} {'Open':<10} {'Score':>6} {'P&L':>10}  Thesis")
    print(f"  {'-'*14} {'-'*6} {'-'*6} {'-'*10} {'-'*10} {'-'*6} {'-'*10}  {'-'*30}")
    for trade in trades:
        pnl = trade.get("realized_pnl")
        pnl_text = "-" if pnl is None else f"${float(pnl):,.2f}"
        print(
            f"  {trade.get('id',''):<14} {trade.get('status',''):<6} {trade.get('ticker',''):<6} "
            f"{trade.get('strategy',''):<10} {str(trade.get('opened_at','')):<10} "
            f"{float(trade.get('score') or 0):>6.1f} {pnl_text:>10}  {trade.get('thesis','')[:60]}"
        )


def print_stats(stats: dict[str, Any]) -> None:
    print(f"\n{'#'*78}")
    print("# TRADE JOURNAL STATS")
    print(f"{'#'*78}\n")
    print(f"  Open trades:       {stats['open_trades']}")
    print(f"  Closed trades:     {stats['closed_trades']}")
    print(f"  Realized P&L:      ${stats['total_realized_pnl']:,.2f}")
    print(f"  Win rate:          {stats['win_rate']:.1f}%")
    print(f"  Avg P&L/trade:     ${stats['avg_pnl']:,.2f}")
    pf = stats["profit_factor"]
    print(f"  Profit factor:     {pf if pf is not None else 'n/a'}")
    if stats["by_strategy"]:
        print("\n  By strategy:")
        for strategy, row in sorted(stats["by_strategy"].items()):
            print(f"    {strategy:<10} n={row['count']:<3} win={row['win_rate']:>5.1f}% pnl=${row['pnl']:>9,.2f}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; JSON remains dual-written for compatibility")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="Record a newly opened trade")
    p_add.add_argument("--id")
    p_add.add_argument("--ticket-id")
    p_add.add_argument("--ticker", required=True)
    p_add.add_argument("--strategy", required=True)
    p_add.add_argument("--quantity", type=float, default=1)
    p_add.add_argument("--entry-credit", type=float, default=0.0)
    p_add.add_argument("--planned-limit-credit", type=float)
    p_add.add_argument("--entry-debit", type=float, default=0.0)
    p_add.add_argument("--capital-at-risk", type=float, default=0.0)
    p_add.add_argument("--max-loss", type=float, default=0.0)
    p_add.add_argument("--score", type=float)
    p_add.add_argument("--verdict")
    p_add.add_argument("--pop-pct", type=float)
    p_add.add_argument("--ann-roc-pct", type=float)
    p_add.add_argument("--dte", type=int)
    p_add.add_argument("--expiration")
    p_add.add_argument("--strikes")
    p_add.add_argument("--opened-at")
    p_add.add_argument("--thesis")
    p_add.add_argument("--tags")
    p_add.add_argument("--json", action="store_true")

    p_close = sub.add_parser("close", help="Close an existing trade and compute realized P&L")
    p_close.add_argument("--id", required=True)
    p_close.add_argument("--exit-credit", type=float, default=0.0)
    p_close.add_argument("--exit-debit", type=float, default=0.0)
    p_close.add_argument("--closed-at")
    p_close.add_argument("--note")
    p_close.add_argument("--json", action="store_true")

    p_list = sub.add_parser("list", help="List journaled trades")
    p_list.add_argument("--status", choices=["OPEN", "CLOSED", "ALL"], default="OPEN")
    p_list.add_argument("--json", action="store_true")

    p_stats = sub.add_parser("stats", help="Summarize realized journal performance")
    p_stats.add_argument("--json", action="store_true")

    p_profiles = sub.add_parser("profiles", help="Show ticker/strategy performance profiles")
    p_profiles.add_argument("--section", choices=["strategy", "ticker", "ticker_strategy"], default="ticker_strategy")
    p_profiles.add_argument("--json", action="store_true")

    return ap


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    state_file = Path(args.state_file)

    if args.cmd in {"add", "close"}:
        # Hold the journal lock across load-mutate-save so a concurrent
        # operator run or second manual command cannot interleave writes.
        with state_lock("journal"):
            state, con = load_backend(state_file, args.db)
            trade = add_trade(state, args) if args.cmd == "add" else close_trade(state, args)
            save_backend(state_file, state, con)
        if args.json:
            print(json.dumps(trade, indent=2, default=str))
        elif args.cmd == "add":
            print(f"Added trade {trade['id']} ({trade['ticker']} {trade['strategy']})")
        else:
            print(f"Closed trade {trade['id']}: realized P&L ${trade['realized_pnl']:,.2f}")
        if con is not None:
            con.close()
        return

    state, con = load_backend(state_file, args.db)
    if args.cmd == "list":
        trades = filter_trades(state.get("trades", []), args.status)
        if args.json:
            print(json.dumps(trades, indent=2, default=str))
        else:
            print_trade_table(trades)
    elif args.cmd == "stats":
        stats = journal_stats(state.get("trades", []))
        if args.json:
            print(json.dumps(stats, indent=2, default=str))
        else:
            print_stats(stats)
    elif args.cmd == "profiles":
        from performance_profiles import build_profiles, print_profiles

        profiles = build_profiles(state.get("trades", []))
        if args.json:
            print(json.dumps(profiles, indent=2, default=str))
        else:
            print_profiles(profiles, args.section)
    if con is not None:
        con.close()


if __name__ == "__main__":
    main()
