#!/usr/bin/env python3.12
"""Measure POP calibration and monthly account performance against SPY."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_journal
from trade_stats import closed_trades, trade_pnl


POP_BUCKETS = ((0, 54), (55, 64), (65, 74), (75, 84), (85, 100))


def pop_bucket(value: float) -> str:
    for low, high in POP_BUCKETS:
        if low <= value <= high:
            return f"{low}-{high}"
    return "UNKNOWN"


def pop_calibration(trades: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        try:
            pop = float(trade.get("pop_pct"))
        except (TypeError, ValueError):
            continue
        buckets[pop_bucket(pop)].append(trade)
    rows = {}
    for name, items in sorted(buckets.items()):
        expected = sum(float(item["pop_pct"]) for item in items) / len(items)
        wins = sum(1 for item in items if trade_pnl(item) > 0)
        realized = wins / len(items) * 100
        rows[name] = {
            "count": len(items),
            "expected_pop_pct": round(expected, 1),
            "realized_win_rate_pct": round(realized, 1),
            "calibration_error_pct": round(realized - expected, 1),
        }
    return {
        "status": "AVAILABLE" if rows else "NO_POP_HISTORY",
        "sample_size": sum(row["count"] for row in rows.values()),
        "buckets": rows,
    }


def premium_collected(trade: dict[str, Any]) -> float:
    quantity = float(trade.get("quantity") or 1)
    net_credit = float(trade.get("entry_credit") or 0) - float(trade.get("entry_debit") or 0)
    return max(0.0, net_credit * quantity * 100)


def monthly_performance(
    trades: list[dict[str, Any]],
    account_nav: float,
    spy_returns: dict[str, float] | None = None,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        month = str(trade.get("closed_at") or "")[:7]
        if len(month) == 7:
            grouped[month].append(trade)
    rows = {}
    for month, items in sorted(grouped.items()):
        pnl = sum(trade_pnl(item) for item in items)
        premium = sum(premium_collected(item) for item in items)
        strategy_return = pnl / account_nav * 100 if account_nav else 0.0
        spy_return = (spy_returns or {}).get(month)
        rows[month] = {
            "closed_trades": len(items),
            "realized_pnl": round(pnl, 2),
            "account_return_pct": round(strategy_return, 2),
            "premium_collected": round(premium, 2),
            "theta_capture_efficiency_pct": round(pnl / premium * 100, 1) if premium else None,
            "spy_return_pct": round(spy_return, 2) if spy_return is not None else None,
            "excess_return_vs_spy_pct": (
                round(strategy_return - spy_return, 2) if spy_return is not None else None
            ),
        }
    return rows


def fetch_spy_monthly_returns(trades: list[dict[str, Any]]) -> dict[str, float]:
    if not trades:
        return {}
    try:
        import yfinance as yf
    except ImportError:
        return {}
    starts = [str(trade.get("opened_at") or trade.get("closed_at") or "")[:10] for trade in trades]
    starts = [value for value in starts if value]
    if not starts:
        return {}
    start = (date.fromisoformat(min(starts)) - timedelta(days=40)).isoformat()
    end = (date.today() + timedelta(days=1)).isoformat()
    try:
        frame = yf.download("SPY", start=start, end=end, auto_adjust=True, progress=False)
    except Exception:
        return {}
    if frame is None or frame.empty:
        return {}
    close = frame["Close"]
    if getattr(close, "ndim", 1) > 1:
        close = close.iloc[:, 0]
    monthly = close.resample("ME").last()
    returns = monthly.pct_change() * 100
    return {
        index.strftime("%Y-%m"): float(value)
        for index, value in returns.dropna().items()
    }


def build_scorecard(
    state: dict[str, Any],
    account_nav: float = 30000,
    spy_returns: dict[str, float] | None = None,
) -> dict[str, Any]:
    trades = closed_trades(state.get("trades", []))
    resolved_spy = fetch_spy_monthly_returns(trades) if spy_returns is None else spy_returns
    return {
        "generated_at": date.today().isoformat(),
        "account_nav": account_nav,
        "pop_calibration": pop_calibration(trades),
        "monthly": monthly_performance(trades, account_nav, resolved_spy),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    parser.add_argument("--db")
    parser.add_argument("--account-nav", type=float, default=30000)
    parser.add_argument("--output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_scorecard(
        load_journal(Path(args.journal), args.db),
        account_nav=args.account_nav,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print(f"POP calibration: {report['pop_calibration']['status']}")
    for month, row in report["monthly"].items():
        spy = "n/a" if row["spy_return_pct"] is None else f"{row['spy_return_pct']:+.2f}%"
        print(
            f"{month}: P&L ${row['realized_pnl']:,.2f}, "
            f"account {row['account_return_pct']:+.2f}%, SPY {spy}"
        )


if __name__ == "__main__":
    main()
