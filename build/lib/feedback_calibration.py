#!/usr/bin/env python3.12
"""Compare planned decisions with realized outcomes and recommend calibration."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from historical_analytics import build_analytics
from trade_journal import DEFAULT_STATE_FILE, load_journal


SCORE_BAND_FLOORS = {"<50": 0, "50-59": 50, "60-69": 60, "70+": 70}


def execution_feedback(trades: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for trade in trades:
        planned = trade.get("planned_limit_credit")
        actual = trade.get("entry_credit")
        if planned is None or actual is None:
            continue
        slippage = float(actual) - float(planned)
        rows.append(
            {
                "trade_id": trade.get("id"),
                "ticket_id": trade.get("ticket_id"),
                "ticker": trade.get("ticker"),
                "slippage_credit": round(slippage, 3),
            }
        )
    avg = sum(row["slippage_credit"] for row in rows) / len(rows) if rows else 0.0
    return {
        "matched_trades": len(rows),
        "avg_credit_vs_plan": round(avg, 3),
        "fill_quality": "GOOD" if rows and avg >= 0 else "WEAK" if rows else "NO_DATA",
        "trades": rows,
    }


def recommended_min_score(
    score_bands: dict[str, dict[str, Any]],
    current_min_score: float,
    min_samples: int,
) -> tuple[float, str]:
    eligible = []
    sampled_bad = []
    for name, row in score_bands.items():
        if int(row.get("count", 0) or 0) < min_samples:
            continue
        if float(row.get("expectancy", 0) or 0) > 0 and float(row.get("win_rate", 0) or 0) >= 50:
            eligible.append(SCORE_BAND_FLOORS.get(name, 100))
        elif float(row.get("expectancy", 0) or 0) < 0:
            sampled_bad.append(SCORE_BAND_FLOORS.get(name, 0))
    if eligible:
        floor = max(50.0, float(min(eligible)), float(max(sampled_bad) + 10) if sampled_bad else 0.0)
        floor = min(75.0, floor)
        return floor, f"lowest profitable score band with at least {min_samples} trades"

    if sampled_bad:
        return min(75.0, max(current_min_score, float(max(sampled_bad) + 10))), "raised above sampled negative-expectancy bands"
    return current_min_score, "insufficient evidence to change threshold"


def build_feedback_report(
    state: dict[str, Any],
    *,
    current_min_score: float = 55.0,
    min_samples: int = 5,
    execution_attribution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    analytics = build_analytics(state)
    score_floor, reason = recommended_min_score(
        analytics.get("by_score_band", {}),
        current_min_score,
        min_samples,
    )
    strategy_adjustments = {}
    for strategy, row in analytics.get("by_strategy", {}).items():
        count = int(row.get("count", 0) or 0)
        expectancy = float(row.get("expectancy", 0) or 0)
        if count < min_samples:
            multiplier = 1.0
            signal = "HOLD"
        elif expectancy < 0:
            multiplier = 0.6
            signal = "THROTTLE"
        elif float(row.get("avg_return_on_risk_pct", 0) or 0) >= 2:
            multiplier = 1.1
            signal = "BOOST"
        else:
            multiplier = 0.9
            signal = "CAUTIOUS"
        strategy_adjustments[strategy] = {
            "signal": signal,
            "multiplier": multiplier,
            "sample_size": count,
            "expectancy": expectancy,
        }

    return {
        "current_min_score": current_min_score,
        "recommended_min_score": score_floor,
        "threshold_reason": reason,
        "min_samples": min_samples,
        "score_bands": analytics.get("by_score_band", {}),
        "strategy_adjustments": strategy_adjustments,
        "execution": execution_feedback(state.get("trades", [])),
        "execution_attribution": execution_attribution or {},
        "execution_adjustments": (execution_attribution or {}).get("strategy_adjustments", {}),
    }


def print_feedback(report: dict[str, Any]) -> None:
    print(f"\n{'#'*78}\n# OUTCOME FEEDBACK CALIBRATION\n{'#'*78}\n")
    print(
        f"  Minimum score: current={report['current_min_score']:.1f} "
        f"recommended={report['recommended_min_score']:.1f}"
    )
    print(f"  Reason: {report['threshold_reason']}")
    execution = report["execution"]
    print(
        f"  Execution matches={execution['matched_trades']} "
        f"avg credit vs plan={execution['avg_credit_vs_plan']:+.3f} ({execution['fill_quality']})"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--current-min-score", type=float, default=55.0)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--db")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    execution_attribution = None
    if args.db:
        from execution_attribution import build_execution_attribution, load_execution_records

        execution_attribution = build_execution_attribution(
            load_execution_records(args.db),
            min_samples=args.min_samples,
        )
    report = build_feedback_report(
        load_journal(Path(args.journal), args.db),
        current_min_score=args.current_min_score,
        min_samples=args.min_samples,
        execution_attribution=execution_attribution,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_feedback(report)


if __name__ == "__main__":
    main()
