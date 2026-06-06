#!/usr/bin/env python3.12
"""Convert realized performance and drawdown into bounded sizing advice."""
from __future__ import annotations

from typing import Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def select_history(analytics: dict[str, Any], ticker: str, strategy: str) -> tuple[str, dict[str, Any]]:
    combo = analytics.get("by_ticker_strategy", {}).get(f"{ticker.upper()}|{strategy.upper()}")
    if combo and int(combo.get("count", 0)) >= 5:
        return "ticker_strategy", combo
    strat = analytics.get("by_strategy", {}).get(strategy.upper())
    if strat and int(strat.get("count", 0)) >= 5:
        return "strategy", strat
    return "overall", analytics.get("overall", {})


def adaptive_size(
    ticker: str,
    strategy: str,
    analytics: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = config or {}
    min_trades = int(cfg.get("min_trades", 5))
    min_multiplier = float(cfg.get("min_multiplier", 0.25))
    max_multiplier = float(cfg.get("max_multiplier", 1.15))
    drawdown_limit = float(cfg.get("drawdown_limit_pct", 8.0))

    scope, history = select_history(analytics, ticker, strategy)
    count = int(history.get("count", 0) or 0)
    expectancy_ror = float(history.get("avg_return_on_risk_pct", 0) or 0)
    profit_factor = history.get("profit_factor")
    recent = analytics.get("recent", {})
    recent_expectancy = float(recent.get("expectancy", 0) or 0)
    overall = analytics.get("overall", {})
    total_capital = max(float(overall.get("total_capital_at_risk", 0) or 0), 1.0)
    current_drawdown = float(analytics.get("drawdown", {}).get("current_drawdown", 0) or 0)
    drawdown_pct = current_drawdown / total_capital * 100

    if count < min_trades:
        performance_factor = 1.0
        evidence = "insufficient history"
    elif expectancy_ror < 0 or (profit_factor is not None and float(profit_factor) < 0.8):
        performance_factor = 0.55
        evidence = "negative realized edge"
    elif expectancy_ror >= 2.0 and (profit_factor is None or float(profit_factor) >= 1.5):
        performance_factor = 1.10
        evidence = "positive realized edge"
    else:
        performance_factor = 0.90
        evidence = "mixed realized edge"

    if drawdown_pct >= drawdown_limit:
        drawdown_factor = 0.50
    elif drawdown_pct >= drawdown_limit / 2:
        drawdown_factor = 0.75
    else:
        drawdown_factor = 1.0

    recent_factor = 0.80 if int(recent.get("count", 0) or 0) >= min_trades and recent_expectancy < 0 else 1.0
    multiplier = clamp(performance_factor * drawdown_factor * recent_factor, min_multiplier, max_multiplier)
    return {
        "multiplier": round(multiplier, 3),
        "scope": scope,
        "sample_size": count,
        "expectancy_ror_pct": round(expectancy_ror, 2),
        "profit_factor": profit_factor,
        "drawdown_pct_proxy": round(drawdown_pct, 2),
        "factors": {
            "performance": performance_factor,
            "drawdown": drawdown_factor,
            "recent": recent_factor,
        },
        "note": f"{evidence}; {scope} n={count}; sizing x{multiplier:.2f}",
    }
