#!/usr/bin/env python3.12
"""Build durable execution-cost attribution from reconciliation history."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from storage import DEFAULT_DB_FILE, connect


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_execution_records(db_path: str | Path) -> list[dict[str, Any]]:
    con = connect(db_path)
    try:
        runs = con.execute(
            "SELECT id, created_at, payload_json FROM reconciliation_runs ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    latest: dict[str, dict[str, Any]] = {}
    for run in runs:
        report = json.loads(run["payload_json"])
        for match in report.get("ticket_matches", []):
            ticket_id = str(match.get("ticket_id") or "")
            if not ticket_id:
                continue
            latest[ticket_id] = {
                **match,
                "reconciliation_run_id": int(run["id"]),
                "reconciled_at": run["created_at"],
            }
    return list(latest.values())


def summarize_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "count": 0,
            "completed": 0,
            "partial": 0,
            "fill_rate": 0.0,
            "quantity_fill_rate": 0.0,
            "avg_credit_improvement": 0.0,
            "total_fees": 0.0,
            "fees_per_contract": 0.0,
            "avg_fill_delay_seconds": None,
        }
    completed = [row for row in records if row.get("status") in {"MATCHED", "OVERFILLED"}]
    partial = [row for row in records if row.get("status") == "PARTIAL"]
    target = sum(as_float(row.get("target_quantity")) or 1.0 for row in records)
    filled = sum(
        min(
            as_float(row.get("filled_quantity")) or 0.0,
            as_float(row.get("target_quantity")) or 1.0,
        )
        for row in records
    )
    improvements = []
    for row in records:
        planned = as_float(row.get("planned_limit_credit"))
        actual = as_float(row.get("fill_price"))
        if planned is not None and actual is not None:
            improvements.append(actual - planned)
    fees = sum(as_float(row.get("fees")) or 0.0 for row in records)
    delays = [
        value
        for value in (as_float(row.get("fill_delay_seconds")) for row in records)
        if value is not None
    ]
    return {
        "count": len(records),
        "completed": len(completed),
        "partial": len(partial),
        "fill_rate": round(len(completed) / len(records) * 100, 1),
        "quantity_fill_rate": round(filled / target * 100, 1) if target else 0.0,
        "avg_credit_improvement": round(sum(improvements) / len(improvements), 4) if improvements else 0.0,
        "total_fees": round(fees, 4),
        "fees_per_contract": round(fees / filled, 4) if filled else 0.0,
        "avg_fill_delay_seconds": round(sum(delays) / len(delays), 1) if delays else None,
    }


def grouped(records: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        buckets[str(key_fn(record) or "UNKNOWN").upper()].append(record)
    return {key: summarize_records(rows) for key, rows in sorted(buckets.items())}


def adjustment_for_summary(summary: dict[str, Any], min_samples: int = 5) -> dict[str, Any]:
    count = int(summary.get("count", 0) or 0)
    if count < min_samples:
        return {
            "score_adjustment": 0.0,
            "size_multiplier": 1.0,
            "signal": "INSUFFICIENT",
            "sample_size": count,
            "reasons": [f"requires {min_samples} samples"],
        }
    points = 0.0
    reasons = []
    fill_rate = float(summary.get("fill_rate", 0) or 0)
    improvement = float(summary.get("avg_credit_improvement", 0) or 0)
    fees = float(summary.get("fees_per_contract", 0) or 0)
    delay = summary.get("avg_fill_delay_seconds")
    if fill_rate < 60:
        points -= 3
        reasons.append("fill rate below 60%")
    elif fill_rate < 80:
        points -= 1.5
        reasons.append("fill rate below 80%")
    elif fill_rate >= 95:
        points += 1
        reasons.append("fill rate at least 95%")
    if improvement < -0.10:
        points -= 2
        reasons.append("credit slippage worse than $0.10")
    elif improvement < -0.03:
        points -= 1
        reasons.append("negative credit slippage")
    elif improvement > 0.03:
        points += 1
        reasons.append("positive credit improvement")
    if fees > 2.0:
        points -= 1
        reasons.append("fees above $2 per contract")
    if delay is not None and float(delay) > 1800:
        points -= 1
        reasons.append("average fill delay above 30 minutes")
    points = max(-5.0, min(5.0, points))
    if points <= -3:
        signal, multiplier = "THROTTLE", 0.75
    elif points < 0:
        signal, multiplier = "CAUTIOUS", 0.9
    elif points >= 2:
        signal, multiplier = "BOOST", 1.05
    else:
        signal, multiplier = "NEUTRAL", 1.0
    return {
        "score_adjustment": points,
        "size_multiplier": multiplier,
        "signal": signal,
        "sample_size": count,
        "reasons": reasons or ["execution near plan"],
    }


def build_execution_attribution(
    records: list[dict[str, Any]],
    min_samples: int = 5,
) -> dict[str, Any]:
    by_strategy = grouped(records, lambda row: row.get("strategy"))
    by_ticker = grouped(records, lambda row: row.get("ticker"))
    by_combo = grouped(
        records,
        lambda row: f"{row.get('ticker') or 'UNKNOWN'}|{row.get('strategy') or 'UNKNOWN'}",
    )
    return {
        "summary": summarize_records(records),
        "min_samples": min_samples,
        "by_strategy": by_strategy,
        "by_ticker": by_ticker,
        "by_ticker_strategy": by_combo,
        "strategy_adjustments": {
            key: {**adjustment_for_summary(value, min_samples), "metrics": value}
            for key, value in by_strategy.items()
        },
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB_FILE))
    parser.add_argument("--min-samples", type=int, default=5)
    parser.add_argument("--output")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    report = build_execution_attribution(
        load_execution_records(args.db),
        min_samples=args.min_samples,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    summary = report["summary"]
    print(
        f"Execution history: n={summary['count']} fill={summary['fill_rate']:.1f}% "
        f"credit_vs_plan={summary['avg_credit_improvement']:+.3f} "
        f"fees/contract=${summary['fees_per_contract']:.2f}"
    )


if __name__ == "__main__":
    main()
