#!/usr/bin/env python3.12
"""
candidate_scoring.py - reusable scoring for option strategy candidates.

The scorer is intentionally transparent: every candidate gets component scores
and a short rationale so the final ranking can be audited before capital is put
at risk.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScoreWeights:
    premium: float = 0.22
    probability: float = 0.20
    liquidity: float = 0.18
    risk_reward: float = 0.16
    volatility: float = 0.14
    timing: float = 0.10


DEFAULT_WEIGHTS = ScoreWeights()


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_range(value: float | None, low: float, high: float) -> float:
    if value is None:
        return 50.0
    if high <= low:
        return 50.0
    return clamp((value - low) / (high - low) * 100)


def score_peak(value: float | None, target: float, tolerance: float) -> float:
    if value is None:
        return 50.0
    return clamp(100 - (abs(value - target) / tolerance * 100))


def parse_earnings_days(metrics: dict[str, Any]) -> int | None:
    raw_date = metrics.get("earnings", {}).get("next")
    if not raw_date:
        return None
    try:
        return (date.fromisoformat(str(raw_date)[:10]) - date.today()).days
    except ValueError:
        return None


def liquidity_score(candidate: dict[str, Any]) -> float:
    volume = candidate.get("volume", candidate.get("volume_short", 0)) or 0
    oi = candidate.get("open_interest", candidate.get("open_interest_short", 0)) or 0
    bid = candidate.get("bid")
    ask = candidate.get("ask")

    volume_component = score_range(float(volume), 0, 500)
    oi_component = score_range(float(oi), 25, 1000)

    if bid is not None and ask is not None and ask > 0:
        mid = (float(bid) + float(ask)) / 2
        spread_pct = ((float(ask) - float(bid)) / mid * 100) if mid > 0 else 100
        spread_component = 100 - score_range(spread_pct, 5, 35)
    else:
        spread_component = 55.0

    return round(clamp(volume_component * 0.25 + oi_component * 0.45 + spread_component * 0.30), 1)


def premium_score(candidate: dict[str, Any]) -> float:
    ann_roc = candidate.get("ann_roc_pct")
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "BULL_PUT":
        return round(score_range(float(ann_roc or 0), 5, 45), 1)
    return round(score_range(float(ann_roc or 0), 2, 35), 1)


def probability_score(candidate: dict[str, Any]) -> float:
    pop = candidate.get("pop_pct")
    if pop is None:
        delta = abs(float(candidate.get("delta", candidate.get("delta_short", 0)) or 0))
        pop = (1 - delta) * 100 if delta else 50
    return round(score_peak(float(pop), target=72, tolerance=28), 1)


def risk_reward_score(candidate: dict[str, Any]) -> float:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "BULL_PUT":
        return round(score_range(float(candidate.get("ratio", 0) or 0), 0.12, 0.45), 1)

    distance = float(candidate.get("distance_to_strike_pct", 0) or 0)
    credit = float(candidate.get("credit", 0) or 0)
    strike = float(candidate.get("strike", 0) or 0)
    credit_pct = (credit / strike * 100) if strike > 0 else 0
    return round(clamp(score_range(distance, 2, 12) * 0.65 + score_range(credit_pct, 0.25, 2.5) * 0.35), 1)


def volatility_score(metrics: dict[str, Any], candidate: dict[str, Any]) -> float:
    iv_rank = metrics.get("iv_rank_proxy_pct")
    if iv_rank is None:
        iv = candidate.get("iv_pct", candidate.get("iv_short_pct"))
        rv = metrics.get("rv_21d_pct")
        if iv is not None and rv:
            iv_rank = (float(iv) / max(float(rv), 1.0)) * 50
    return round(score_peak(float(iv_rank), target=60, tolerance=45), 1) if iv_rank is not None else 50.0


def timing_score(ticker_dte: int, metrics: dict[str, Any]) -> float:
    dte_score = score_peak(float(ticker_dte), target=35, tolerance=28)
    earnings_days = parse_earnings_days(metrics)
    if earnings_days is None:
        earnings_score = 90.0
    elif earnings_days <= 3:
        earnings_score = 15.0
    elif earnings_days <= 10:
        earnings_score = 45.0
    elif earnings_days <= 21:
        earnings_score = 65.0
    else:
        earnings_score = 90.0
    return round(dte_score * 0.55 + earnings_score * 0.45, 1)


def verdict_for_score(score: float) -> str:
    if score >= 80:
        return "DEPLOY"
    if score >= 68:
        return "SMALL_SIZE"
    if score >= 55:
        return "WATCH"
    return "SKIP"


def rationale_from_components(components: dict[str, float], candidate: dict[str, Any]) -> list[str]:
    rationale = []
    strong = [name for name, value in components.items() if value >= 75]
    weak = [name for name, value in components.items() if value < 45]
    if strong:
        rationale.append("Strengths: " + ", ".join(strong[:3]))
    if weak:
        rationale.append("Watch: " + ", ".join(weak[:3]))
    if candidate.get("ann_roc_pct") is not None:
        rationale.append(f"AnnROC={candidate['ann_roc_pct']:.1f}%")
    if candidate.get("pop_pct") is not None:
        rationale.append(f"POP={candidate['pop_pct']:.0f}%")
    return rationale


def score_candidate(
    ticker: str,
    candidate: dict[str, Any],
    metrics: dict[str, Any],
    dte: int,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> dict[str, Any]:
    components = {
        "premium": premium_score(candidate),
        "probability": probability_score(candidate),
        "liquidity": liquidity_score(candidate),
        "risk_reward": risk_reward_score(candidate),
        "volatility": volatility_score(metrics, candidate),
        "timing": timing_score(dte, metrics),
    }
    score = (
        components["premium"] * weights.premium
        + components["probability"] * weights.probability
        + components["liquidity"] * weights.liquidity
        + components["risk_reward"] * weights.risk_reward
        + components["volatility"] * weights.volatility
        + components["timing"] * weights.timing
    )
    scored = dict(candidate)
    scored.update({
        "ticker": ticker,
        "score": round(score, 1),
        "verdict": verdict_for_score(score),
        "score_components": components,
        "score_rationale": rationale_from_components(components, candidate),
    })
    return scored


def score_results(results: dict[str, Any]) -> dict[str, Any]:
    ranked = []
    for ticker, data in results.get("tickers", {}).items():
        metrics = data.get("metrics", {})
        dte = int(data.get("dte") or 0)
        for strategy, rows in data.get("strategies", {}).items():
            scored_rows = [score_candidate(ticker, row, metrics, dte) for row in rows]
            scored_rows.sort(key=lambda row: row["score"], reverse=True)
            data["strategies"][strategy] = scored_rows
            ranked.extend(scored_rows)
    ranked.sort(key=lambda row: row["score"], reverse=True)
    results["ranked_candidates"] = ranked
    return results


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("report", help="Path to an options_screener JSON report")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = json.loads(Path(args.report).read_text())
    scored = score_results(results)

    if args.json:
        print(json.dumps(scored, indent=2, default=str))
        return

    print(f"\n{'#'*78}")
    print("# RANKED OPTION CANDIDATES")
    print(f"{'#'*78}\n")
    print(f"  {'Score':>5} {'Verdict':<10} {'Ticker':<6} {'Strategy':<9} {'ROC':>7} {'POP':>6}  Rationale")
    for row in scored.get("ranked_candidates", [])[:args.top]:
        print(
            f"  {row['score']:>5.1f} {row['verdict']:<10} {row['ticker']:<6} "
            f"{row.get('strategy', ''):<9} {row.get('ann_roc_pct', 0):>6.1f}% "
            f"{row.get('pop_pct', 0):>5.1f}%  {' | '.join(row.get('score_rationale', []))}"
        )


if __name__ == "__main__":
    main()
