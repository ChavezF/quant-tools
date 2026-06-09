#!/usr/bin/env python3.12
"""Select a risk-budgeted basket from an action plan."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from toolkit_config import add_config_argument, load_config


DEFAULT_LIMITS: dict[str, Any] = {
    "max_positions": 6,
    "max_total_capital_pct": 0.35,
    "max_tail_loss_pct": 0.08,
    "max_ticker_capital_pct": 0.15,
    "max_group_exposure_pct": 0.35,
    "stress_loss_fraction": 0.65,
    "include_reduce": True,
}


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def candidate_key(action: dict[str, Any]) -> str:
    candidate = action.get("candidate", {})
    strategy = str(action.get("strategy") or candidate.get("strategy") or "").upper()
    if strategy == "BULL_PUT":
        strikes = f"{candidate.get('short_strike')}/{candidate.get('long_strike')}"
    else:
        strikes = str(candidate.get("strike") or "")
    return "|".join(
        [
            str(action.get("ticker") or "").upper(),
            strategy,
            str(candidate.get("expiration") or ""),
            strikes,
        ]
    )


def objective_score(action: dict[str, Any]) -> float:
    candidate = action.get("candidate", {})
    execution = candidate.get("execution", {})
    score = as_float(action.get("score"))
    pop = as_float(candidate.get("pop_pct"))
    ann_roc = min(100.0, max(0.0, as_float(candidate.get("ann_roc_pct"))))
    execution_score = as_float(execution.get("execution_score"))
    correlation_penalty = as_float(action.get("correlation", {}).get("penalty"))
    value = score * 0.55 + pop * 0.15 + ann_roc * 0.15 + execution_score * 0.15
    return round(value - correlation_penalty * 20, 2)


def current_portfolio_delta(actions: list[dict[str, Any]]) -> float:
    for action in actions:
        if action.get("projected_delta") is not None and action.get("delta_change") is not None:
            return as_float(action.get("projected_delta")) - as_float(action.get("delta_change"))
    return 0.0


def allocate_portfolio(plan: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    configured = config or {}
    limits = {name: configured.get(name, default) for name, default in DEFAULT_LIMITS.items()}
    account_nav = as_float(plan.get("limits", {}).get("account_nav"))
    max_delta = as_float(plan.get("limits", {}).get("max_portfolio_delta_abs"))
    actions = plan.get("actions", [])
    allowed = {"APPROVE", "REDUCE"} if limits["include_reduce"] else {"APPROVE"}
    eligible = [action for action in actions if action.get("action_decision") in allowed]
    eligible.sort(key=lambda action: (-objective_score(action), as_float(action.get("max_loss"))))

    capital_budget = account_nav * as_float(limits["max_total_capital_pct"])
    tail_budget = account_nav * as_float(limits["max_tail_loss_pct"])
    ticker_budget = account_nav * as_float(limits["max_ticker_capital_pct"])
    group_budget_pct = as_float(limits["max_group_exposure_pct"])
    stress_fraction = as_float(limits["stress_loss_fraction"])
    base_delta = current_portfolio_delta(actions)

    selected: list[dict[str, Any]] = []
    selected_actions: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    ticker_capital: dict[str, float] = {}
    group_capital: dict[str, float] = {}
    group_baseline: dict[str, float] = {}
    total_capital = 0.0
    total_tail_loss = 0.0
    delta_change = 0.0

    for action in eligible:
        ticker = str(action.get("ticker") or "").upper()
        multiplier = max(0.0, as_float(action.get("action_size_multiplier"), 1.0))
        capital = as_float(action.get("capital_required")) * multiplier
        max_loss = as_float(action.get("max_loss")) * multiplier
        tail_loss = max_loss * stress_fraction
        candidate_delta = as_float(action.get("delta_change")) * multiplier
        correlation = action.get("correlation", {})
        groups = correlation.get("groups", []) or []
        dominant_group = correlation.get("dominant_group")
        if dominant_group:
            group_baseline[dominant_group] = max(
                group_baseline.get(dominant_group, 0.0),
                as_float(correlation.get("group_exposure")),
            )

        reasons = []
        if capital <= 0 or max_loss <= 0:
            reasons.append("missing positive capital or max-loss estimate")
        if len(selected) >= int(limits["max_positions"]):
            reasons.append(f"position limit {int(limits['max_positions'])}")
        if total_capital + capital > capital_budget:
            reasons.append(f"capital budget ${capital_budget:,.0f}")
        if total_tail_loss + tail_loss > tail_budget:
            reasons.append(f"tail-loss budget ${tail_budget:,.0f}")
        if ticker_capital.get(ticker, 0.0) + capital > ticker_budget:
            reasons.append(f"{ticker} allocation budget ${ticker_budget:,.0f}")
        projected_delta = base_delta + delta_change + candidate_delta
        if max_delta > 0 and abs(projected_delta) > max_delta:
            reasons.append(f"portfolio delta limit +/-{max_delta:.0f}")
        for group in groups:
            projected_group = (
                group_baseline.get(group, 0.0)
                + group_capital.get(group, 0.0)
                + capital
            )
            if account_nav > 0 and projected_group / account_nav > group_budget_pct:
                reasons.append(f"{group} exposure limit {group_budget_pct * 100:.0f}% NAV")

        key = candidate_key(action)
        if reasons:
            excluded.append(
                {
                    "candidate_key": key,
                    "ticker": ticker,
                    "strategy": action.get("strategy"),
                    "objective_score": objective_score(action),
                    "reasons": reasons,
                }
            )
            continue

        total_capital += capital
        total_tail_loss += tail_loss
        delta_change += candidate_delta
        ticker_capital[ticker] = ticker_capital.get(ticker, 0.0) + capital
        for group in groups:
            group_capital[group] = group_capital.get(group, 0.0) + capital

        allocation = {
            "candidate_key": key,
            "rank": len(selected) + 1,
            "objective_score": objective_score(action),
            "capital": round(capital, 2),
            "tail_loss": round(tail_loss, 2),
            "delta_change": round(candidate_delta, 2),
        }
        selected.append(
            {
                **allocation,
                "ticker": ticker,
                "strategy": action.get("strategy"),
                "decision": action.get("action_decision"),
            }
        )
        selected_action = deepcopy(action)
        selected_action["portfolio_allocation"] = allocation
        selected_actions.append(selected_action)

    projected_delta = base_delta + delta_change
    return {
        "limits": {
            **limits,
            "account_nav": account_nav,
            "capital_budget": round(capital_budget, 2),
            "tail_loss_budget": round(tail_budget, 2),
        },
        "summary": {
            "eligible": len(eligible),
            "selected": len(selected),
            "excluded": len(excluded),
            "capital_allocated": round(total_capital, 2),
            "capital_utilization_pct": round(total_capital / capital_budget * 100, 2) if capital_budget else 0.0,
            "tail_loss_allocated": round(total_tail_loss, 2),
            "tail_budget_utilization_pct": round(total_tail_loss / tail_budget * 100, 2) if tail_budget else 0.0,
            "base_delta": round(base_delta, 2),
            "projected_delta": round(projected_delta, 2),
        },
        "selected": selected,
        "excluded": excluded,
        "actions": selected_actions,
    }


def print_allocation(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#' * 78}\n# PORTFOLIO ALLOCATION\n{'#' * 78}\n")
    print(
        f"  Selected {summary['selected']}/{summary['eligible']} | "
        f"capital ${summary['capital_allocated']:,.0f} "
        f"({summary['capital_utilization_pct']:.1f}% of budget) | "
        f"tail ${summary['tail_loss_allocated']:,.0f} "
        f"({summary['tail_budget_utilization_pct']:.1f}% of budget)"
    )
    for row in report["selected"]:
        print(
            f"  {row['rank']:>2}. {row['ticker']:<6} {str(row['strategy']):<10} "
            f"objective={row['objective_score']:>5.1f} capital=${row['capital']:>8,.0f} "
            f"tail=${row['tail_loss']:>7,.0f}"
        )
    if report["excluded"]:
        print("\n  Excluded:")
        for row in report["excluded"][:10]:
            print(f"    {row['ticker']} {row['strategy']}: {'; '.join(row['reasons'])}")


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--plan", required=True, help="Path to action_plan --json output")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    report = allocate_portfolio(
        json.loads(Path(args.plan).read_text()),
        cfg.get("portfolio_allocation", {}),
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_allocation(report)


if __name__ == "__main__":
    main()
