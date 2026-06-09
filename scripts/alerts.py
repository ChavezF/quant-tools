#!/usr/bin/env python3.12
"""Generate actionable alerts from action plans and the trade journal."""
from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_state


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def alert(priority: str, kind: str, title: str, detail: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "priority": priority,
        "kind": kind,
        "title": title,
        "detail": detail,
        "payload": payload or {},
    }


def candidate_alerts(plan: dict[str, Any], min_score: float) -> list[dict[str, Any]]:
    out = []
    for row in plan.get("actions", []):
        action = row.get("action_decision")
        score = float(row.get("score") or 0)
        if action not in {"APPROVE", "REDUCE"} or score < min_score:
            continue
        candidate = row.get("candidate", {})
        execution = candidate.get("execution", {})
        title = f"{action}: {row.get('ticker')} {row.get('strategy')} score {score:.1f}"
        detail = (
            f"size={row.get('action_size_multiplier', 0):.2f}, "
            f"limit={execution.get('suggested_limit_credit', 0):.2f}, "
            f"floor={execution.get('do_not_chase_below', 0):.2f}, "
            f"exec={execution.get('execution_grade', '?')}"
        )
        out.append(alert("HIGH" if action == "APPROVE" else "MEDIUM", "candidate", title, detail, row))
    return out


def journal_alerts(journal_state: dict[str, Any], profit_target_pct: float, dte_warning: int) -> list[dict[str, Any]]:
    out = []
    today = date.today()
    for trade in journal_state.get("trades", []):
        if trade.get("status") != "OPEN":
            continue
        trade_id = trade.get("id")
        ticker = trade.get("ticker")
        strategy = trade.get("strategy")
        pnl_pct = trade.get("unrealized_pnl_pct")
        if pnl_pct is not None and float(pnl_pct) >= profit_target_pct:
            out.append(alert(
                "HIGH",
                "profit_target",
                f"Profit target hit: {ticker} {strategy}",
                f"{trade_id} unrealized P&L {float(pnl_pct):.1f}% >= {profit_target_pct:.1f}%",
                trade,
            ))

        exp = parse_date(trade.get("expiration"))
        if exp:
            dte = (exp - today).days
            if dte <= dte_warning:
                out.append(alert(
                    "MEDIUM" if dte > 7 else "HIGH",
                    "dte_warning",
                    f"DTE warning: {ticker} {strategy}",
                    f"{trade_id} expires in {dte} days",
                    trade,
                ))
    return out


def model_health_alerts(
    validation: dict[str, Any] | None,
    drift: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    out = []
    validation_summary = (validation or {}).get("summary", {})
    validation_status = validation_summary.get("status")
    if validation_status in {"FAIL", "WATCH"}:
        priority = "HIGH" if validation_status == "FAIL" else "MEDIUM"
        out.append(
            alert(
                priority,
                "validation",
                f"Walk-forward validation: {validation_status}",
                f"profitable folds={float(validation_summary.get('profitable_fold_pct', 0) or 0):.1f}%, "
                f"OOS expectancy=${float(validation_summary.get('avg_oos_expectancy', 0) or 0):,.2f}",
                validation_summary,
            )
        )

    drift_summary = (drift or {}).get("summary", {})
    if drift_summary.get("status") == "DRIFT":
        priority = "HIGH" if drift_summary.get("severity") == "HIGH" else "MEDIUM"
        comparison = (drift or {}).get("comparison", {})
        out.append(
            alert(
                priority,
                "drift",
                f"Performance drift: {drift_summary.get('severity')}",
                f"expectancy change=${float(comparison.get('expectancy_change', 0) or 0):+,.2f}, "
                f"win-rate change={float(comparison.get('win_rate_change', 0) or 0):+.1f} pts",
                drift,
            )
        )
    return out


def build_alerts(
    plan: dict[str, Any] | None,
    journal_state: dict[str, Any] | None,
    min_score: float,
    profit_target_pct: float,
    dte_warning: int,
    validation: dict[str, Any] | None = None,
    drift: dict[str, Any] | None = None,
) -> dict[str, Any]:
    alerts = []
    if plan:
        alerts.extend(candidate_alerts(plan, min_score))
    if journal_state:
        alerts.extend(journal_alerts(journal_state, profit_target_pct, dte_warning))
    alerts.extend(model_health_alerts(validation, drift))

    priority_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    alerts.sort(key=lambda row: (priority_order.get(row["priority"], 9), row["kind"], row["title"]))
    return {
        "as_of": datetime.now().isoformat(),
        "summary": {
            "total": len(alerts),
            "high": sum(1 for row in alerts if row["priority"] == "HIGH"),
            "medium": sum(1 for row in alerts if row["priority"] == "MEDIUM"),
            "low": sum(1 for row in alerts if row["priority"] == "LOW"),
        },
        "alerts": alerts,
    }


def print_alerts(report: dict[str, Any]) -> None:
    print(f"\n{'#'*78}")
    print("# QUANT ALERTS")
    print(f"{'#'*78}\n")
    summary = report["summary"]
    print(f"  Summary: total={summary['total']} high={summary['high']} medium={summary['medium']} low={summary['low']}")
    if not report["alerts"]:
        print("\n  No alerts.")
        return
    print(f"\n  {'Pri':<6} {'Kind':<14} Alert")
    print(f"  {'-'*6} {'-'*14} {'-'*50}")
    for row in report["alerts"]:
        print(f"  {row['priority']:<6} {row['kind']:<14} {row['title']} - {row['detail']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="Path to action_plan --json output")
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE), help="Path to trade journal state")
    ap.add_argument("--min-score", type=float, default=68.0)
    ap.add_argument("--profit-target-pct", type=float, default=50.0)
    ap.add_argument("--dte-warning", type=int, default=21)
    ap.add_argument("--validation")
    ap.add_argument("--drift")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text()) if args.plan else None
    journal_path = Path(args.journal)
    journal_state = load_state(journal_path) if journal_path.exists() else None
    validation = json.loads(Path(args.validation).read_text()) if args.validation and Path(args.validation).exists() else None
    drift = json.loads(Path(args.drift).read_text()) if args.drift and Path(args.drift).exists() else None
    report = build_alerts(
        plan,
        journal_state,
        args.min_score,
        args.profit_target_pct,
        args.dte_warning,
        validation,
        drift,
    )
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print_alerts(report)


if __name__ == "__main__":
    main()
