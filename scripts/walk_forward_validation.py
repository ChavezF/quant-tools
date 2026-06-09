#!/usr/bin/env python3.12
"""Walk-forward validation for live journal score thresholds."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from historical_analytics import closed_trades, summarize_trades
from trade_journal import DEFAULT_STATE_FILE, load_state


DEFAULT_THRESHOLDS = [50.0, 55.0, 60.0, 65.0, 70.0, 75.0]


def threshold_stats(trades: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    selected = [trade for trade in trades if float(trade.get("score") or 0) >= threshold]
    return summarize_trades(selected)


def choose_threshold(
    train: list[dict[str, Any]],
    thresholds: list[float],
    min_selected: int,
) -> tuple[float, dict[str, Any]]:
    candidates = []
    for threshold in thresholds:
        stats = threshold_stats(train, threshold)
        if int(stats.get("count", 0)) < min_selected:
            continue
        objective = (
            float(stats.get("avg_return_on_risk_pct", 0) or 0),
            float(stats.get("expectancy", 0) or 0),
            float(stats.get("win_rate", 0) or 0),
            -threshold,
        )
        candidates.append((objective, threshold, stats))
    if not candidates:
        threshold = min(thresholds)
        return threshold, threshold_stats(train, threshold)
    _, threshold, stats = max(candidates, key=lambda row: row[0])
    return threshold, stats


def validate_scope(
    trades: list[dict[str, Any]],
    *,
    min_train: int,
    test_window: int,
    thresholds: list[float],
    min_selected: int,
) -> dict[str, Any]:
    folds = []
    start = min_train
    while start < len(trades):
        train = trades[:start]
        test = trades[start : start + test_window]
        if not test:
            break
        threshold, train_stats = choose_threshold(train, thresholds, min_selected)
        test_stats = threshold_stats(test, threshold)
        folds.append(
            {
                "train_count": len(train),
                "test_count": len(test),
                "test_start": test[0].get("closed_at"),
                "test_end": test[-1].get("closed_at"),
                "selected_threshold": threshold,
                "train_selected": train_stats,
                "test_selected": test_stats,
            }
        )
        start += test_window

    valid = [fold for fold in folds if int(fold["test_selected"].get("count", 0)) > 0]
    thresholds_used = [float(fold["selected_threshold"]) for fold in valid]
    expectancies = [float(fold["test_selected"].get("expectancy", 0) or 0) for fold in valid]
    profitable = sum(1 for value in expectancies if value > 0)
    profitable_pct = profitable / len(valid) * 100 if valid else 0.0
    avg_expectancy = mean(expectancies) if expectancies else 0.0
    threshold_std = pstdev(thresholds_used) if len(thresholds_used) > 1 else 0.0

    if not valid:
        status = "INSUFFICIENT_DATA"
    elif profitable_pct >= 60 and avg_expectancy > 0:
        status = "PASS"
    elif profitable_pct >= 40 or avg_expectancy >= 0:
        status = "WATCH"
    else:
        status = "FAIL"

    return {
        "status": status,
        "fold_count": len(folds),
        "valid_fold_count": len(valid),
        "profitable_fold_pct": round(profitable_pct, 1),
        "avg_oos_expectancy": round(avg_expectancy, 2),
        "avg_selected_threshold": round(mean(thresholds_used), 1) if thresholds_used else None,
        "threshold_std": round(threshold_std, 2),
        "folds": folds,
    }


def build_walk_forward_report(
    state: dict[str, Any],
    *,
    min_train: int = 10,
    test_window: int = 5,
    thresholds: list[float] | None = None,
    min_selected: int = 3,
) -> dict[str, Any]:
    trades = closed_trades(state.get("trades", []))
    threshold_grid = thresholds or DEFAULT_THRESHOLDS
    overall = validate_scope(
        trades,
        min_train=min_train,
        test_window=test_window,
        thresholds=threshold_grid,
        min_selected=min_selected,
    )
    strategies = {}
    for strategy in sorted({str(trade.get("strategy") or "UNKNOWN").upper() for trade in trades}):
        rows = [trade for trade in trades if str(trade.get("strategy") or "UNKNOWN").upper() == strategy]
        strategies[strategy] = validate_scope(
            rows,
            min_train=min_train,
            test_window=test_window,
            thresholds=threshold_grid,
            min_selected=min_selected,
        )
    return {
        "config": {
            "min_train": min_train,
            "test_window": test_window,
            "thresholds": threshold_grid,
            "min_selected": min_selected,
        },
        "summary": {
            "status": overall["status"],
            "closed_trades": len(trades),
            "profitable_fold_pct": overall["profitable_fold_pct"],
            "avg_oos_expectancy": overall["avg_oos_expectancy"],
            "avg_selected_threshold": overall["avg_selected_threshold"],
            "threshold_std": overall["threshold_std"],
        },
        "overall": overall,
        "by_strategy": strategies,
    }


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#' * 78}\n# WALK-FORWARD VALIDATION\n{'#' * 78}\n")
    print(
        f"  {summary['status']} | trades={summary['closed_trades']} | "
        f"profitable folds={summary['profitable_fold_pct']:.1f}% | "
        f"OOS expectancy=${summary['avg_oos_expectancy']:,.2f}"
    )
    print(
        f"  Average threshold={summary['avg_selected_threshold'] or 'n/a'} | "
        f"threshold std={summary['threshold_std']:.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--min-train", type=int, default=10)
    ap.add_argument("--test-window", type=int, default=5)
    ap.add_argument("--thresholds", nargs="+", type=float, default=DEFAULT_THRESHOLDS)
    ap.add_argument("--min-selected", type=int, default=3)
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_walk_forward_report(
        load_state(Path(args.journal)),
        min_train=args.min_train,
        test_window=args.test_window,
        thresholds=args.thresholds,
        min_selected=args.min_selected,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
