#!/usr/bin/env python3.12
"""Execution-quality and slippage estimates for option candidates."""
from __future__ import annotations

from typing import Any


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def midpoint(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return (bid + ask) / 2


def spread_pct(bid: float | None, ask: float | None) -> float | None:
    mid = midpoint(bid, ask)
    if mid is None:
        return None
    return max(0.0, (float(ask) - float(bid)) / mid * 100)


def grade_from_score(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def score_from_spread(spread: float | None) -> float:
    if spread is None:
        return 45.0
    if spread <= 5:
        return 100.0
    if spread >= 40:
        return 15.0
    return clamp(100 - ((spread - 5) / 35 * 85))


def score_from_depth(volume: float, open_interest: float) -> float:
    volume_score = clamp(volume / 500 * 100)
    oi_score = clamp(max(0.0, open_interest - 25) / 975 * 100)
    return volume_score * 0.35 + oi_score * 0.65


def single_leg_execution(candidate: dict[str, Any]) -> dict[str, Any]:
    bid = float(candidate.get("bid") or 0)
    ask = float(candidate.get("ask") or 0)
    mid = midpoint(bid, ask)
    spread = spread_pct(bid, ask)
    credit = float(candidate.get("credit") or candidate.get("mark") or 0)
    volume = float(candidate.get("volume") or 0)
    oi = float(candidate.get("open_interest") or 0)

    depth_score = score_from_depth(volume, oi)
    spread_score = score_from_spread(spread)
    score = clamp(spread_score * 0.60 + depth_score * 0.40)
    half_spread = ((ask - bid) / 2) if mid is not None else 0.0
    slippage_estimate = max(0.0, half_spread * 0.35)
    suggested_limit = max(0.01, credit - slippage_estimate)

    return {
        "execution_score": round(score, 1),
        "execution_grade": grade_from_score(score),
        "bid_ask_spread_pct": round(spread, 2) if spread is not None else None,
        "mark_confidence": "HIGH" if mid is not None and spread is not None and spread <= 10 else "MEDIUM" if mid is not None else "LOW",
        "estimated_slippage": round(slippage_estimate, 2),
        "suggested_limit_credit": round(suggested_limit, 2),
        "do_not_chase_below": round(max(0.01, suggested_limit - slippage_estimate), 2),
    }


def spread_execution(candidate: dict[str, Any]) -> dict[str, Any]:
    volume = float(candidate.get("volume_short") or candidate.get("volume") or 0)
    oi = float(candidate.get("open_interest_short") or candidate.get("open_interest") or 0)
    depth_score = score_from_depth(volume, oi)
    ratio = float(candidate.get("ratio") or 0)
    credit = float(candidate.get("credit") or 0)

    # Multi-leg spread bid/ask is unavailable in current candidate schema, so
    # grade conservatively from short-leg depth and credit/max-loss quality.
    ratio_score = clamp((ratio - 0.12) / 0.33 * 100) if ratio else 45.0
    score = clamp(depth_score * 0.55 + ratio_score * 0.45)
    slippage_estimate = max(0.02, credit * 0.08)
    suggested_limit = max(0.01, credit - slippage_estimate)

    return {
        "execution_score": round(score, 1),
        "execution_grade": grade_from_score(score),
        "bid_ask_spread_pct": None,
        "mark_confidence": "MEDIUM" if score >= 55 else "LOW",
        "estimated_slippage": round(slippage_estimate, 2),
        "suggested_limit_credit": round(suggested_limit, 2),
        "do_not_chase_below": round(max(0.01, suggested_limit - slippage_estimate), 2),
    }


def execution_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy in {"CSP", "CC"}:
        return single_leg_execution(candidate)
    if strategy == "BULL_PUT":
        return spread_execution(candidate)
    return {
        "execution_score": 45.0,
        "execution_grade": "D",
        "bid_ask_spread_pct": None,
        "mark_confidence": "LOW",
        "estimated_slippage": 0.0,
        "suggested_limit_credit": float(candidate.get("credit") or 0),
        "do_not_chase_below": float(candidate.get("credit") or 0),
    }


def attach_execution_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    enriched = dict(candidate)
    enriched["execution"] = execution_quality(candidate)
    return enriched
