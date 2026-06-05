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

from candidate_scoring import score_results
from performance_profiles import build_profiles, lookup_profile, profile_note
from pretrade_check import RiskLimits, evaluate_report
from trade_journal import DEFAULT_STATE_FILE, journal_stats, load_state


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

    out["action_decision"] = adjusted
    out["action_size_multiplier"] = multiplier
    out["performance_note"] = note
    out["profile_scope"] = profile_scope
    out["profile_signal"] = profile_signal
    out["profile_note"] = profile_note(profile_scope, profile)
    return out


def build_action_plan(
    screener_report: dict[str, Any],
    portfolio_report: dict[str, Any] | None,
    journal_state: dict[str, Any] | None,
    limits: RiskLimits,
) -> dict[str, Any]:
    score_results(screener_report)
    risk_report = evaluate_report(screener_report, portfolio_report, limits)
    stats_by_strategy = strategy_stats_map(journal_state)
    profiles = build_profiles(journal_state.get("trades", [])) if journal_state else {}
    actions = [
        apply_performance_overlay(decision, stats_by_strategy, profiles)
        for decision in risk_report.get("decisions", [])
    ]

    approved = [a for a in actions if a["action_decision"] == "APPROVE"]
    reduced = [a for a in actions if a["action_decision"] == "REDUCE"]
    rejected = [a for a in actions if a["action_decision"] == "REJECT"]

    return {
        "limits": risk_report["limits"],
        "journal_stats": journal_stats(journal_state.get("trades", [])) if journal_state else None,
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
    args = ap.parse_args()

    limits = RiskLimits(
        account_nav=args.account_nav,
        max_trade_risk_pct=args.max_trade_risk_pct,
        max_trade_bp_pct=args.max_trade_bp_pct,
        max_single_ticker_pct=args.max_single_ticker_pct,
        max_portfolio_delta_abs=args.max_portfolio_delta_abs,
        min_score=args.min_score,
        min_liquidity_score=args.min_liquidity_score,
        min_pop_pct=args.min_pop_pct,
    )
    screener_report = json.loads(Path(args.candidates).read_text())
    portfolio_report = json.loads(Path(args.portfolio).read_text()) if args.portfolio else None
    journal_path = Path(args.journal)
    journal_state = load_state(journal_path) if journal_path.exists() else None

    plan = build_action_plan(screener_report, portfolio_report, journal_state, limits)
    if args.json:
        print(json.dumps(plan, indent=2, default=str))
        return
    print_action_plan(plan, args.top)


if __name__ == "__main__":
    main()
