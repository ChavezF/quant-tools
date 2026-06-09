#!/usr/bin/env python3.12
"""
action_plan.py - combine scan, pre-trade risk, and journal data into one plan.

This produces the operator view: what is approved, what should be reduced, what
is rejected, and how current realized performance should temper deployment.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from adaptive_sizing import adaptive_size
from candidate_scoring import score_results
from correlation_risk import correlation_penalty
from feedback_calibration import build_feedback_report
from historical_analytics import build_analytics
from performance_profiles import build_profiles, lookup_profile, profile_note
from pretrade_check import RiskLimits, evaluate_report
from common import derive_live_account_nav
from trade_journal import DEFAULT_STATE_FILE, journal_stats, load_state
from toolkit_config import add_config_argument, load_config


def strategy_stats_map(journal_state: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not journal_state:
        return {}
    return journal_stats(journal_state.get("trades", [])).get("by_strategy", {})


def performance_note(strategy: str, stats_by_strategy: dict[str, dict[str, Any]]) -> str:
    stats = stats_by_strategy.get(strategy.upper())
    if not stats:
        return "no live history"
    count = int(stats.get("count", 0) or 0)
    win_rate = float(stats.get("win_rate", 0) or 0)
    pnl = float(stats.get("pnl", 0) or 0)
    if count < 5:
        return f"limited live history: n={count}, win={win_rate:.1f}%, pnl=${pnl:,.0f}"
    if pnl < 0 or win_rate < 45:
        return f"throttle: n={count}, win={win_rate:.1f}%, pnl=${pnl:,.0f}"
    if win_rate >= 60 and pnl > 0:
        return f"confirmed live edge: n={count}, win={win_rate:.1f}%, pnl=${pnl:,.0f}"
    return f"neutral live history: n={count}, win={win_rate:.1f}%, pnl=${pnl:,.0f}"


def apply_performance_overlay(
    decision: dict[str, Any],
    stats_by_strategy: dict[str, dict[str, Any]],
    profiles: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    adaptive: dict[str, Any] | None = None,
    feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out = dict(decision)
    strategy = str(out.get("strategy") or "").upper()
    ticker = str(out.get("ticker") or "").upper()
    stats = stats_by_strategy.get(strategy, {})
    count = int(stats.get("count", 0) or 0)
    win_rate = float(stats.get("win_rate", 0) or 0)
    pnl = float(stats.get("pnl", 0) or 0)
    note = performance_note(strategy, stats_by_strategy)

    adjusted = out["risk_decision"]
    multiplier = float(out.get("size_multiplier", 0) or 0)
    if count >= 5 and (pnl < 0 or win_rate < 45):
        if adjusted == "APPROVE":
            adjusted = "REDUCE"
            multiplier = min(multiplier, 0.5)
        elif adjusted == "REDUCE":
            multiplier = min(multiplier, 0.25)

    profile_scope, profile = lookup_profile(profiles or {}, ticker, strategy)
    profile_signal = profile.get("signal") if profile else "NO_HISTORY"
    if profile_signal == "THROTTLE":
        if adjusted == "APPROVE":
            adjusted = "REDUCE"
            multiplier = min(multiplier, 0.5)
        elif adjusted == "REDUCE":
            multiplier = min(multiplier, 0.25)
    elif profile_signal == "BOOST" and adjusted == "REDUCE" and not any(
        not check["ok"] and check["severity"] == "hard" for check in out.get("checks", [])
    ):
        adjusted = "APPROVE"
        multiplier = max(multiplier, 0.75)

    if correlation and correlation.get("penalty", 0) > 0:
        if adjusted == "APPROVE":
            adjusted = "REDUCE"
        multiplier = max(0.0, multiplier * (1 - float(correlation["penalty"])))

    if adaptive:
        multiplier = max(0.0, multiplier * float(adaptive.get("multiplier", 1.0)))
        if adjusted == "APPROVE" and float(adaptive.get("multiplier", 1.0)) < 0.75:
            adjusted = "REDUCE"

    calibrated_floor = float((feedback or {}).get("recommended_min_score", 0) or 0)
    if adjusted != "REJECT" and calibrated_floor > 0 and float(out.get("score") or 0) < calibrated_floor:
        adjusted = "REDUCE"
        multiplier = min(multiplier, 0.5)

    out["action_decision"] = adjusted
    out["action_size_multiplier"] = round(multiplier, 3)
    out["performance_note"] = note
    out["profile_scope"] = profile_scope
    out["profile_signal"] = profile_signal
    out["profile_note"] = profile_note(profile_scope, profile)
    out["correlation"] = correlation or {}
    out["adaptive_sizing"] = adaptive or {}
    out["feedback_calibration"] = {
        "recommended_min_score": calibrated_floor,
        "threshold_reason": (feedback or {}).get("threshold_reason"),
    }
    return out


def build_action_plan(
    screener_report: dict[str, Any],
    portfolio_report: dict[str, Any] | None,
    journal_state: dict[str, Any] | None,
    limits: RiskLimits,
    correlation_groups: dict[str, list[str]] | None = None,
    adaptive_config: dict[str, Any] | None = None,
    feedback_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score_results(screener_report)
    risk_report = evaluate_report(screener_report, portfolio_report, limits)
    stats_by_strategy = strategy_stats_map(journal_state)
    trades = journal_state.get("trades", []) if journal_state else []
    profiles = build_profiles(trades)
    analytics = build_analytics(journal_state or {"trades": []})
    feedback = build_feedback_report(
        journal_state or {"trades": []},
        current_min_score=limits.min_score,
        min_samples=int((feedback_config or {}).get("min_samples", 5)),
    )
    actions = []
    for decision in risk_report.get("decisions", []):
        corr = correlation_penalty(
            str(decision.get("ticker") or ""),
            portfolio_report,
            correlation_groups or {},
            limits.account_nav,
        )
        sizing = adaptive_size(
            str(decision.get("ticker") or ""),
            str(decision.get("strategy") or ""),
            analytics,
            adaptive_config,
        )
        actions.append(apply_performance_overlay(decision, stats_by_strategy, profiles, corr, sizing, feedback))

    approved = [a for a in actions if a["action_decision"] == "APPROVE"]
    reduced = [a for a in actions if a["action_decision"] == "REDUCE"]
    rejected = [a for a in actions if a["action_decision"] == "REJECT"]

    return {
        "limits": risk_report["limits"],
        "journal_stats": journal_stats(journal_state.get("trades", [])) if journal_state else None,
        "historical_analytics": analytics,
        "feedback_calibration": feedback,
        "performance_profiles": profiles,
        "summary": {
            "approve": len(approved),
            "reduce": len(reduced),
            "reject": len(rejected),
        },
        "actions": actions,
    }


def print_action_plan(plan: dict[str, Any], top: int) -> None:
    print(f"\n{'#'*78}")
    print("# DAILY ACTION PLAN")
    print(f"{'#'*78}\n")
    summary = plan["summary"]
    print(f"  Summary: APPROVE={summary['approve']}  REDUCE={summary['reduce']}  REJECT={summary['reject']}")

    journal = plan.get("journal_stats")
    if journal:
        print(
            f"  Live stats: closed={journal['closed_trades']} win={journal['win_rate']:.1f}% "
            f"pnl=${journal['total_realized_pnl']:,.2f}"
        )

    actionable = [a for a in plan["actions"] if a["action_decision"] in ("APPROVE", "REDUCE")]
    print(f"\n  {'Action':<8} {'Size':>4} {'Score':>5} {'Ticker':<6} {'Strategy':<9} "
          f"{'Limit':>7} {'Floor':>7}  Notes")
    print(f"  {'-'*8} {'-'*4} {'-'*5} {'-'*6} {'-'*9} {'-'*7} {'-'*7}  {'-'*30}")
    for row in actionable[:top]:
        execution = row.get("candidate", {}).get("execution", {})
        print(
            f"  {row['action_decision']:<8} {row['action_size_multiplier']:>4.2f} "
            f"{float(row.get('score') or 0):>5.1f} {row['ticker']:<6} "
            f"{str(row.get('strategy') or ''):<9} {execution.get('suggested_limit_credit', 0):>7.2f} "
            f"{execution.get('do_not_chase_below', 0):>7.2f}  "
            f"exec={execution.get('execution_grade', '?')} | {row['profile_note']}"
        )

    rejected = [a for a in plan["actions"] if a["action_decision"] == "REJECT"]
    if rejected:
        print("\n  Top rejects:")
        for row in rejected[: min(5, top)]:
            failed = [check["name"] for check in row["checks"] if not check["ok"]]
            print(
                f"    {row['ticker']} {row.get('strategy')}: score={float(row.get('score') or 0):.1f}, "
                f"failed={', '.join(failed)}"
            )


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--candidates", required=True, help="Path to options_screener JSON report")
    ap.add_argument("--portfolio", help="Optional path to portfolio_risk --json output")
    ap.add_argument("--journal", default=str(DEFAULT_STATE_FILE), help="Path to trade journal state")
    ap.add_argument("--account-nav", type=float, default=RiskLimits.account_nav)
    ap.add_argument("--max-trade-risk-pct", type=float, default=RiskLimits.max_trade_risk_pct)
    ap.add_argument("--max-trade-bp-pct", type=float, default=RiskLimits.max_trade_bp_pct)
    ap.add_argument("--max-single-ticker-pct", type=float, default=RiskLimits.max_single_ticker_pct)
    ap.add_argument("--max-portfolio-delta-abs", type=float, default=RiskLimits.max_portfolio_delta_abs)
    ap.add_argument("--min-score", type=float, default=RiskLimits.min_score)
    ap.add_argument("--min-liquidity-score", type=float, default=RiskLimits.min_liquidity_score)
    ap.add_argument("--min-pop-pct", type=float, default=RiskLimits.min_pop_pct)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--correlation-groups", help="Optional JSON file containing correlation_groups")
    args = ap.parse_args()

    screener_report = json.loads(Path(args.candidates).read_text())
    portfolio_report = json.loads(Path(args.portfolio).read_text()) if args.portfolio else None
    journal_path = Path(args.journal)
    journal_state = load_state(journal_path) if journal_path.exists() else None
    cfg = load_config(args.config)
    correlation_groups = cfg.get("correlation_groups", {})
    if args.correlation_groups:
        correlation_groups = json.loads(Path(args.correlation_groups).read_text())

    # Prefer the live NAV from the risk report so account-size changes
    # (deposits, withdrawals, P&L) flow through automatically without code
    # changes. Falls back to the CLI value (which defaults to 30000) when no
    # report is supplied.
    live_nav = derive_live_account_nav(portfolio_report, args.account_nav)
    limits = RiskLimits(
        account_nav=live_nav,
        max_trade_risk_pct=args.max_trade_risk_pct,
        max_trade_bp_pct=args.max_trade_bp_pct,
        max_single_ticker_pct=args.max_single_ticker_pct,
        max_portfolio_delta_abs=args.max_portfolio_delta_abs,
        min_score=args.min_score,
        min_liquidity_score=args.min_liquidity_score,
        min_pop_pct=args.min_pop_pct,
    )

    plan = build_action_plan(
        screener_report,
        portfolio_report,
        journal_state,
        limits,
        correlation_groups,
        cfg.get("adaptive_sizing", {}),
        cfg.get("feedback", {}),
    )
    if args.json:
        print(json.dumps(plan, indent=2, default=str))
        return
    print_action_plan(plan, args.top)


if __name__ == "__main__":
    main()
