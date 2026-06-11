#!/usr/bin/env python3.12
"""Deterministic portfolio scenario stress tests from a saved risk report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_SCENARIOS = [
    {"name": "soft_pullback", "market_shock_pct": -3.0, "vol_shock_pct": 5.0},
    {"name": "risk_off", "market_shock_pct": -7.0, "vol_shock_pct": 12.0},
    {"name": "crash", "market_shock_pct": -15.0, "vol_shock_pct": 25.0},
    {"name": "relief_rally", "market_shock_pct": 5.0, "vol_shock_pct": -5.0},
]


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def scenario_nav(portfolio_report: dict[str, Any], risk: dict[str, Any]) -> float:
    total = as_float(risk.get("total_value"))
    if total:
        return abs(total)
    positions = portfolio_report.get("positions", [])
    return abs(sum(as_float(pos.get("current_value")) for pos in positions))


def position_stress(position: dict[str, Any], scenario: dict[str, Any], portfolio_beta: float) -> dict[str, Any]:
    shock = as_float(scenario.get("market_shock_pct")) / 100
    vol_shock = as_float(scenario.get("vol_shock_pct")) / 100
    current_value = as_float(position.get("current_value"))
    quantity = as_float(position.get("quantity"), 1.0)
    pos_type = str(position.get("type") or "").upper()
    symbol = str(position.get("symbol") or "")

    if pos_type == "OPTION":
        greeks = position.get("greeks", {})
        spot = as_float(position.get("underlying_price"))
        sign = 1 if quantity >= 0 else -1
        multiplier = abs(quantity) * 100
        has_greeks_model = bool(spot and any(as_float(greeks.get(name)) for name in ("delta", "gamma", "vega")))
        if has_greeks_model:
            delta_pnl = as_float(greeks.get("delta")) * multiplier * sign * spot * shock
            gamma_pnl = 0.5 * as_float(greeks.get("gamma")) * multiplier * ((spot * shock) ** 2) * sign
            vega_pnl = as_float(greeks.get("vega")) * multiplier * (vol_shock * 100) * sign
            pnl = delta_pnl + gamma_pnl + vega_pnl
            model = "greeks"
        else:
            pnl = current_value * shock * portfolio_beta
            model = "value_fallback"
    else:
        pnl = current_value * shock * portfolio_beta
        model = "notional_beta"

    return {
        "symbol": symbol,
        "type": pos_type or "UNKNOWN",
        "pnl": round(pnl, 2),
        "current_value": round(current_value, 2),
        "model": model,
    }


def run_scenario(portfolio_report: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    portfolio = portfolio_report.get("portfolio", {})
    risk = portfolio_report.get("risk", {})
    positions = portfolio.get("positions", []) or portfolio_report.get("positions", [])
    beta = as_float(risk.get("portfolio_beta"), 1.0) or 1.0
    nav = scenario_nav(portfolio, risk)
    rows = [position_stress(position, scenario, beta) for position in positions]
    total_pnl = sum(row["pnl"] for row in rows)
    rows.sort(key=lambda row: row["pnl"])
    return {
        "name": scenario.get("name"),
        "market_shock_pct": as_float(scenario.get("market_shock_pct")),
        "vol_shock_pct": as_float(scenario.get("vol_shock_pct")),
        "estimated_pnl": round(total_pnl, 2),
        "estimated_pnl_pct_nav": round(total_pnl / nav * 100, 2) if nav else 0.0,
        "worst_positions": rows[:5],
    }


def build_scenario_report(
    portfolio_report: dict[str, Any],
    scenarios: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    scenario_rows = [run_scenario(portfolio_report, scenario) for scenario in (scenarios or DEFAULT_SCENARIOS)]
    worst = min(scenario_rows, key=lambda row: row["estimated_pnl"], default={})
    return {
        "summary": {
            "scenario_count": len(scenario_rows),
            "worst_scenario": worst.get("name"),
            "worst_pnl": worst.get("estimated_pnl", 0.0),
            "worst_pnl_pct_nav": worst.get("estimated_pnl_pct_nav", 0.0),
        },
        "scenarios": scenario_rows,
    }


def load_scenarios(path: str | None) -> list[dict[str, Any]] | None:
    if not path:
        return None
    payload = json.loads(Path(path).read_text())
    scenarios = payload if isinstance(payload, list) else payload.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise SystemExit("Scenario file must contain a non-empty JSON list or {'scenarios': [...]}")
    return scenarios


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(f"\n{'#'*78}\n# SCENARIO STRESS\n{'#'*78}\n")
    print(
        f"  Worst: {summary['worst_scenario']} "
        f"${float(summary['worst_pnl'] or 0):,.2f} ({float(summary['worst_pnl_pct_nav'] or 0):.2f}% NAV)"
    )
    for row in report["scenarios"]:
        print(f"  {row['name']:<14} {row['estimated_pnl']:>12,.2f} {row['estimated_pnl_pct_nav']:>7.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--portfolio", required=True, help="Path to portfolio_risk --json output")
    ap.add_argument("--scenarios", help="Optional JSON list or {'scenarios': [...]} file")
    ap.add_argument("--output")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    report = build_scenario_report(json.loads(Path(args.portfolio).read_text()), load_scenarios(args.scenarios))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2, default=str))
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(report)


if __name__ == "__main__":
    main()
