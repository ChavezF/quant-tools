#!/usr/bin/env python3.12
"""Correlation/factor concentration helpers."""
from __future__ import annotations

from typing import Any


def ticker_groups(ticker: str, groups: dict[str, list[str]]) -> list[str]:
    ticker = ticker.upper()
    return [name for name, symbols in groups.items() if ticker in {s.upper() for s in symbols}]


def portfolio_group_exposure(portfolio_report: dict[str, Any] | None, groups: dict[str, list[str]]) -> dict[str, float]:
    exposure = {name: 0.0 for name in groups}
    if not portfolio_report:
        return exposure
    positions = portfolio_report.get("portfolio", {}).get("positions", [])
    for pos in positions:
        symbol = str(pos.get("symbol", "")).upper()
        value = abs(float(pos.get("current_value", 0) or 0))
        for group in ticker_groups(symbol, groups):
            exposure[group] += value
    return exposure


def correlation_penalty(
    ticker: str,
    portfolio_report: dict[str, Any] | None,
    groups: dict[str, list[str]],
    account_nav: float,
    warning_pct: float = 0.25,
) -> dict[str, Any]:
    candidate_groups = ticker_groups(ticker, groups)
    exposure = portfolio_group_exposure(portfolio_report, groups)
    if not candidate_groups or account_nav <= 0:
        return {"penalty": 0.0, "groups": candidate_groups, "note": "no correlation group overlap"}

    max_group = max(candidate_groups, key=lambda group: exposure.get(group, 0.0))
    group_exposure = exposure.get(max_group, 0.0)
    exposure_pct = group_exposure / account_nav
    if exposure_pct >= warning_pct:
        penalty = min(0.5, (exposure_pct - warning_pct) * 1.5 + 0.15)
        note = f"{max_group} exposure already {exposure_pct*100:.1f}% of NAV"
    else:
        penalty = 0.0
        note = f"{max_group} exposure {exposure_pct*100:.1f}% of NAV"
    return {
        "penalty": round(penalty, 2),
        "groups": candidate_groups,
        "dominant_group": max_group,
        "group_exposure": round(group_exposure, 2),
        "group_exposure_pct": round(exposure_pct * 100, 2),
        "note": note,
    }
