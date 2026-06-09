#!/usr/bin/env python3.12
"""Detect live performance and calibration drift from the trade journal."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from feedback_calibration import build_feedback_report
from historical_analytics import closed_trades, summarize_trades
from trade_journal import DEFAULT_STATE_FILE, load_state


def severity_for(expectancy_change: float, win_rate_change: float, ror_change: float) -> str:
    if expectancy_change <= -50 or win_rate_change <= -20 or ror_change <= -3:
        return "HIGH"
    if expectancy_change <= -20 or win_rate_change <= -10 or ror_change <= -1:
        return "MEDIUM"
    return "LOW"


def compare_periods(baseline: dict[str, Any], recent: dict[str, Any]) -> dict[str, Any]:
    expectancy_change = float(recent.get("expectancy", 0) or 0) - float(baseline.get("expectancy", 0) or 0)
    win_rate_change = float(recent.get("win_rate", 0) or 0) - float(baseline.get("win_rate", 0) or 0)
    ror_change = float(recent.get("avg_return_on_risk_pct", 0) or 0) - float(
        baseline.get("avg_return_on_risk_pct", 0) or 0
    )
    return {
        "expectancy_change": round(expectancy_change, 2),
        "win_rate_change": round(win_rate_change, 1),
        "return_on_risk_change": round(ror_change, 2),
        "severity": severity_for(expectancy_change, win_rate_change, ror_change),
    }


def build_drift_report(
    state: dict[str, Any],
    *,
    recent_window: int = 10,
    min_baseline: int = 10,
    current_min_score: float = 55.0,
    min_samples: int = 5,
) -> dict[str, Any]:
    trades = closed_trades(state.get("trades", []))
    recent = trades[-recent_window:]
    baseline = trades[:-recent_window] if len(trades) > recent_window else []
    baseline_stats = summarize_trades(baseline)
    recent_stats = summarize_trades(recent)

    if len(baseline) < min_baseline or not recent:
        comparison = {
            "expectancy_change": 0.0,
            "win_rate_change": 0.0,
            "return_on_risk_change": 0.0,
            "severity": "INSUFFICIENT_DATA",
        }
        score_shift = 0.0
        baseline_feedback = {}
        current_feedback = build_feedback_report(
            {"trades": trades},
            current_min_score=current_min_score,
            min_samples=min_samples,
        )
    else:
        comparison = compare_periods(baseline_stats, recent_stats)
        baseline_feedback = build_feedback_report(
            {"trades": baseline},
            current_min_score=current_min_score,
            min_samples=min_samples,
        )
        current_feedback = build_feedback_report(
            {"trades": trades},
            current_min_score=current_min_score,
            min_samples=min_samples,
        )
        score_shift = float(current_feedback["recommended_min_score"]) - float(
            baseline_feedback["recommended_min_score"]
        )
        if abs(score_shift) >= 10 and comparison["severity"] == "LOW":
            comparison["severity"] = "MEDIUM"

    strategy_changes = []
    baseline_adjustments = baseline_feedback.get("strategy_adjustments", {})
    for strategy, current in current_feedback.get("strategy_adjustments", {}).items():
        previous = baseline_adjustments.get(strategy)
        if previous and previous.get("signal") != current.get("signal"):
            strategy_changes.append(
                {
                    "strategy": strategy,
                    "from": previous.get("signal"),
                    "to": current.get("signal"),
                    "expectancy": current.get("expectancy"),
                }
            )

    severity = comparison["severity"]
    status = "INSUFFICIENT_DATA" if severity == "INSUFFICIENT_DATA" else "DRIFT" if severity in {"HIGH", "MEDIUM"} else "STABLE"
    return {
        "summary": {
            "status": status,
            "severity": severity,
            "baseline_trades": len(baseline),
            "recent_trades": len(recent),
            "score_threshold_shift": round(score_shift, 1),
            "strategy_signal_changes": len(strategy_changes),
        },
        "baseline": baseline_stats,
        "recent": recent_stats,
        "comparison": comparison,
        "calibration": {
            "baseline_min_score": baseline_feedback.get("recommended_min_score"),
            "current_min_score": current_feedback.get("recommended_min_score"),
            "score_threshold_shift": round(score_shift, 1),
            "strategy_signal_changes": strategy_changes,
        },
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    comparison = report["comparison"]
    print(f"\n{'#' * 78}\n# PERFORMANCE DRIFT MONITOR\n{'#' * 78}\n")
    print(
        f"  {summary['status']} ({summary['severity']}) | baseline={summary['baseline_trades']} "
        f"recent={summary['recent_trades']} | score shift={summary['score_threshold_shift']:+.1f}"
    )
    print(
        f"  Expectancy {comparison['expectancy_change']:+.2f} | "
        f"win rate {comparison['win_rate_change']:+.1f} pts | "
        f"return/risk {comparison['return_on_risk_change']:+.2f} pts"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--recent-window", type=int, default=10)
    ap.add_argument("--min-baseline", type=int, default=10)
    ap.add_argument("--current-min-score", type=float, default=55.0)
    ap.add_argument("--min-samples", type=int, default=5)
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_drift_report(
        load_state(Path(args.journal)),
        recent_window=args.recent_window,
        min_baseline=args.min_baseline,
        current_min_score=args.current_min_score,
        min_samples=args.min_samples,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
