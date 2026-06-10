#!/usr/bin/env python3.12
"""Shared realized-performance math for journal-derived reports.

trade_journal.py, historical_analytics.py, and performance_profiles.py each
present different report schemas, but the underlying win/loss/expectancy math
is identical. It lives here so the three reports cannot drift apart.
"""
from __future__ import annotations

from typing import Any


def trade_pnl(trade: dict[str, Any]) -> float:
    return float(trade.get("realized_pnl") or 0)


def closed_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Closed trades in deterministic close order (closed_at, then id)."""
    rows = [trade for trade in trades if trade.get("status") == "CLOSED"]
    return sorted(rows, key=lambda trade: (str(trade.get("closed_at") or ""), str(trade.get("id") or "")))


def pnl_breakdown(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw (unrounded) win/loss totals for a set of closed trades.

    Wins are strictly positive realized P&L; zero counts as a loss, matching
    the long-standing journal convention.
    """
    pnls = [trade_pnl(trade) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl <= 0]
    # float() keeps empty sums as 0.0 so JSON output stays float-typed
    return {
        "count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "total_pnl": float(sum(pnls)),
        "gross_wins": float(sum(wins)),
        "gross_losses": float(abs(sum(losses))),
        "sum_losses": float(sum(losses)),
    }


def win_rate_pct(wins: int, count: int) -> float:
    return round(wins / count * 100, 1) if count else 0.0


def profit_factor(gross_wins: float, gross_losses: float) -> float | None:
    return round(gross_wins / gross_losses, 2) if gross_losses else None
