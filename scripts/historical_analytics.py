#!/usr/bin/env python3.12
"""Build sequence-aware performance analytics from the trade journal."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_journal
from trade_stats import closed_trades, pnl_breakdown, profit_factor, trade_pnl, win_rate_pct


def score_band(score: Any) -> str:
    value = float(score or 0)
    if value >= 70:
        return "70+"
    if value >= 60:
        return "60-69"
    if value >= 50:
        return "50-59"
    return "<50"


def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
    if not trades:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "expectancy": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": None,
            "avg_return_on_risk_pct": 0.0,
            "total_capital_at_risk": 0.0,
        }

    breakdown = pnl_breakdown(trades)
    returns = []
    total_capital = 0.0
    for trade in trades:
        capital = float(trade.get("capital_at_risk") or trade.get("max_loss") or 0)
        if capital > 0:
            total_capital += capital
            returns.append(trade_pnl(trade) / capital * 100)
    return {
        "count": breakdown["count"],
        "wins": breakdown["wins"],
        "losses": breakdown["losses"],
        "win_rate": win_rate_pct(breakdown["wins"], breakdown["count"]),
        "total_pnl": round(breakdown["total_pnl"], 2),
        "expectancy": round(breakdown["total_pnl"] / breakdown["count"], 2),
        "avg_win": round(breakdown["gross_wins"] / breakdown["wins"], 2) if breakdown["wins"] else 0.0,
        "avg_loss": round(breakdown["sum_losses"] / breakdown["losses"], 2) if breakdown["losses"] else 0.0,
        "profit_factor": profit_factor(breakdown["gross_wins"], breakdown["gross_losses"]),
        "avg_return_on_risk_pct": round(sum(returns) / len(returns), 2) if returns else 0.0,
        "total_capital_at_risk": round(total_capital, 2),
    }


def equity_curve(trades: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    curve = []
    for trade in trades:
        equity += trade_pnl(trade)
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown = max(max_drawdown, drawdown)
        curve.append(
            {
                "date": trade.get("closed_at") or trade.get("opened_at"),
                "trade_id": trade.get("id"),
                "ticker": trade.get("ticker"),
                "strategy": trade.get("strategy"),
                "pnl": round(trade_pnl(trade), 2),
                "equity": round(equity, 2),
                "drawdown": round(drawdown, 2),
            }
        )
    current_drawdown = peak - equity
    return curve, {
        "peak_pnl": round(peak, 2),
        "ending_pnl": round(equity, 2),
        "max_drawdown": round(max_drawdown, 2),
        "current_drawdown": round(current_drawdown, 2),
    }


def grouped_summary(trades: list[dict[str, Any]], key_fn) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        buckets[str(key_fn(trade))].append(trade)
    return {key: summarize_trades(rows) for key, rows in sorted(buckets.items())}


def build_analytics(state: dict[str, Any], recent_window: int = 10) -> dict[str, Any]:
    trades = closed_trades(state.get("trades", []))
    curve, drawdown = equity_curve(trades)
    recent = trades[-recent_window:]
    return {
        "generated_at": date.today().isoformat(),
        "overall": summarize_trades(trades),
        "recent": summarize_trades(recent),
        "recent_window": recent_window,
        "drawdown": drawdown,
        "by_strategy": grouped_summary(trades, lambda trade: str(trade.get("strategy") or "UNKNOWN").upper()),
        "by_ticker": grouped_summary(trades, lambda trade: str(trade.get("ticker") or "UNKNOWN").upper()),
        "by_ticker_strategy": grouped_summary(
            trades,
            lambda trade: f"{str(trade.get('ticker') or 'UNKNOWN').upper()}|{str(trade.get('strategy') or 'UNKNOWN').upper()}",
        ),
        "by_score_band": grouped_summary(trades, lambda trade: score_band(trade.get("score"))),
        "equity_curve": curve,
    }


def print_analytics(report: dict[str, Any]) -> None:
    overall = report["overall"]
    drawdown = report["drawdown"]
    recent = report["recent"]
    print(f"\n{'#'*78}\n# HISTORICAL DECISION ANALYTICS\n{'#'*78}\n")
    print(
        f"  Closed={overall['count']}  Win={overall['win_rate']:.1f}%  "
        f"Expectancy=${overall['expectancy']:,.2f}  PF={overall['profit_factor'] or 'n/a'}"
    )
    print(
        f"  P&L=${overall['total_pnl']:,.2f}  Max drawdown=${drawdown['max_drawdown']:,.2f}  "
        f"Current drawdown=${drawdown['current_drawdown']:,.2f}"
    )
    print(
        f"  Recent {report['recent_window']}: n={recent['count']} "
        f"win={recent['win_rate']:.1f}% expectancy=${recent['expectancy']:,.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; authoritative over the JSON journal when set")
    ap.add_argument("--recent-window", type=int, default=10)
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_analytics(load_journal(Path(args.journal), args.db), recent_window=args.recent_window)
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_analytics(report)


if __name__ == "__main__":
    main()
