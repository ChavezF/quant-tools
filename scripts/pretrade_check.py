#!/usr/bin/env python3.12
"""
pretrade_check.py - portfolio-aware risk gate for scored option candidates.

Consumes an options_screener JSON report, ensures candidates are scored, and
checks each candidate against explicit risk limits before it reaches execution.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from candidate_scoring import score_results


@dataclass(frozen=True)
class RiskLimits:
    account_nav: float = 30000.0
    max_trade_risk_pct: float = 0.05
    max_trade_bp_pct: float = 0.20
    max_single_ticker_pct: float = 0.25
    max_portfolio_delta_abs: float = 250.0
    min_score: float = 55.0
    min_liquidity_score: float = 45.0
    min_pop_pct: float = 55.0


def candidate_capital(candidate: dict[str, Any]) -> float:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "BULL_PUT":
        return float(candidate.get("max_loss", 0) or 0)
    return float(candidate.get("capital", 0) or 0)


def candidate_max_loss(candidate: dict[str, Any]) -> float:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "BULL_PUT":
        return float(candidate.get("max_loss", 0) or 0)
    strike = float(candidate.get("strike", 0) or 0)
    credit = float(candidate.get("credit", 0) or 0)
    return max(0.0, (strike - credit) * 100)


def candidate_delta_shares(candidate: dict[str, Any]) -> float:
    strategy = str(candidate.get("strategy", "")).upper()
    if strategy == "CSP":
        return float(candidate.get("delta", 0) or 0) * 100
    if strategy == "CC":
        return float(candidate.get("delta", 0) or 0) * -100
    if strategy == "BULL_PUT":
        return float(candidate.get("delta_short", 0) or 0) * 100
    return 0.0


def current_ticker_exposure(portfolio: dict[str, Any], ticker: str) -> float:
    positions = portfolio.get("positions", [])
    exposure = 0.0
    for pos in positions:
        symbol = str(pos.get("symbol", ""))
        if symbol.startswith(ticker):
            exposure += abs(float(pos.get("current_value", 0) or 0))
    return exposure


def current_delta(risk: dict[str, Any]) -> float:
    return float(risk.get("net_delta_shares", 0) or 0)


def evaluate_candidate(
    candidate: dict[str, Any],
    portfolio_report: dict[str, Any] | None,
    limits: RiskLimits,
) -> dict[str, Any]:
    portfolio = (portfolio_report or {}).get("portfolio", {})
    risk = (portfolio_report or {}).get("risk", {})
    ticker = str(candidate.get("ticker", ""))
    components = candidate.get("score_components", {})

    capital = candidate_capital(candidate)
    max_loss = candidate_max_loss(candidate)
    proposed_delta = candidate_delta_shares(candidate)
    next_delta = current_delta(risk) + proposed_delta
    ticker_exposure = current_ticker_exposure(portfolio, ticker) + capital

    checks = []

    def add_check(name: str, ok: bool, detail: str, severity: str = "hard") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity})

    trade_risk_limit = limits.account_nav * limits.max_trade_risk_pct
    trade_bp_limit = limits.account_nav * limits.max_trade_bp_pct
    ticker_limit = limits.account_nav * limits.max_single_ticker_pct

    add_check(
        "score",
        float(candidate.get("score", 0) or 0) >= limits.min_score,
        f"score {candidate.get('score', 0)} >= {limits.min_score}",
        "soft",
    )
    add_check(
        "liquidity",
        float(components.get("liquidity", 0) or 0) >= limits.min_liquidity_score,
        f"liquidity {components.get('liquidity', 0)} >= {limits.min_liquidity_score}",
    )
    add_check(
        "probability",
        float(candidate.get("pop_pct", 0) or 0) >= limits.min_pop_pct,
        f"POP {candidate.get('pop_pct', 0)} >= {limits.min_pop_pct}",
        "soft",
    )
    add_check(
        "max_trade_risk",
        max_loss <= trade_risk_limit,
        f"max loss ${max_loss:,.0f} <= ${trade_risk_limit:,.0f}",
    )
    add_check(
        "buying_power",
        capital <= trade_bp_limit,
        f"capital/BP ${capital:,.0f} <= ${trade_bp_limit:,.0f}",
    )
    add_check(
        "single_ticker_exposure",
        ticker_exposure <= ticker_limit,
        f"{ticker} exposure ${ticker_exposure:,.0f} <= ${ticker_limit:,.0f}",
    )
    add_check(
        "delta_limit",
        abs(next_delta) <= limits.max_portfolio_delta_abs,
        f"projected delta {next_delta:+.0f} within +/-{limits.max_portfolio_delta_abs:.0f}",
    )

    hard_failures = [c for c in checks if not c["ok"] and c["severity"] == "hard"]
    soft_failures = [c for c in checks if not c["ok"] and c["severity"] == "soft"]

    if hard_failures:
        decision = "REJECT"
    elif soft_failures:
        decision = "REDUCE"
    else:
        decision = "APPROVE"

    size_multiplier = 1.0
    if decision == "REDUCE":
        size_multiplier = 0.5
    elif decision == "REJECT":
        size_multiplier = 0.0

    return {
        "ticker": ticker,
        "strategy": candidate.get("strategy"),
        "score": candidate.get("score"),
        "candidate_verdict": candidate.get("verdict"),
        "risk_decision": decision,
        "size_multiplier": size_multiplier,
        "capital_required": round(capital, 2),
        "max_loss": round(max_loss, 2),
        "delta_change": round(proposed_delta, 2),
        "projected_delta": round(next_delta, 2),
        "checks": checks,
        "candidate": candidate,
    }


def evaluate_report(
    screener_report: dict[str, Any],
    portfolio_report: dict[str, Any] | None,
    limits: RiskLimits,
) -> dict[str, Any]:
    if "ranked_candidates" not in screener_report:
        score_results(screener_report)
    decisions = [
        evaluate_candidate(candidate, portfolio_report, limits)
        for candidate in screener_report.get("ranked_candidates", [])
    ]
    return {
        "limits": asdict(limits),
        "decisions": decisions,
        "summary": {
            "approve": sum(1 for d in decisions if d["risk_decision"] == "APPROVE"),
            "reduce": sum(1 for d in decisions if d["risk_decision"] == "REDUCE"),
            "reject": sum(1 for d in decisions if d["risk_decision"] == "REJECT"),
        },
    }


def print_report(report: dict[str, Any], top: int) -> None:
    print(f"\n{'#'*78}")
    print("# PRE-TRADE RISK CHECK")
    print(f"{'#'*78}\n")
    summary = report["summary"]
    print(f"  Summary: APPROVE={summary['approve']}  REDUCE={summary['reduce']}  REJECT={summary['reject']}")
    print(f"  {'Decision':<8} {'Score':>5} {'Ticker':<6} {'Strategy':<9} {'Capital':>10} {'MaxLoss':>10} {'Delta':>8}  Failed checks")
    print(f"  {'-'*8} {'-'*5} {'-'*6} {'-'*9} {'-'*10} {'-'*10} {'-'*8}  {'-'*25}")
    for row in report["decisions"][:top]:
        failed = [check["name"] for check in row["checks"] if not check["ok"]]
        print(
            f"  {row['risk_decision']:<8} {float(row.get('score') or 0):>5.1f} "
            f"{row['ticker']:<6} {str(row.get('strategy') or ''):<9} "
            f"${row['capital_required']:>9,.0f} ${row['max_loss']:>9,.0f} "
            f"{row['delta_change']:>+8.1f}  {', '.join(failed) if failed else '-'}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidates", required=True, help="Path to options_screener JSON report")
    ap.add_argument("--portfolio", help="Optional path to portfolio_risk --json output")
    ap.add_argument("--account-nav", type=float, default=RiskLimits.account_nav)
    ap.add_argument("--max-trade-risk-pct", type=float, default=RiskLimits.max_trade_risk_pct)
    ap.add_argument("--max-trade-bp-pct", type=float, default=RiskLimits.max_trade_bp_pct)
    ap.add_argument("--max-single-ticker-pct", type=float, default=RiskLimits.max_single_ticker_pct)
    ap.add_argument("--max-portfolio-delta-abs", type=float, default=RiskLimits.max_portfolio_delta_abs)
    ap.add_argument("--min-score", type=float, default=RiskLimits.min_score)
    ap.add_argument("--min-liquidity-score", type=float, default=RiskLimits.min_liquidity_score)
    ap.add_argument("--min-pop-pct", type=float, default=RiskLimits.min_pop_pct)
    ap.add_argument("--top", type=int, default=20)
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
    report = evaluate_report(screener_report, portfolio_report, limits)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print_report(report, args.top)


if __name__ == "__main__":
    main()
