#!/usr/bin/env python3.12
"""
portfolio_risk.py — Live portfolio risk dashboard for Public.com.

Pulls current positions, fetches live Greeks for any options,
and computes:
  - Net long/short delta, gamma, vega, theta exposure
  - Portfolio beta (market-cap weighted)
  - Sector / position concentration
  - Parametric 1-day 95% VaR (delta-normal method)
  - Stress test: what happens if market drops X%

If account is empty, prints a "READY TO TRADE" template showing
  what the risk dashboard WOULD compute on the target watchlist.
  Use --target-watchlist to demo it without a real portfolio.

Usage:
  ./portfolio_risk.py                       # real portfolio
  ./portfolio_risk.py --target-watchlist SPY QQQ NVDA AAPL  # demo
  ./portfolio_risk.py --json                # JSON output
"""
import argparse
import json
import sys
from datetime import datetime
import numpy as np
import yfinance as yf

from common import configure_public_imports, get_public_client, greeks_to_dict, underlying_from_position

configure_public_imports()


def get_client():
    return get_public_client()


def fetch_portfolio(client) -> dict:
    """Return dict with positions list and equity info."""
    try:
        p = client.get_portfolio()
        positions = []
        for pos in (p.positions or []):
            inst = pos.instrument
            positions.append({
                "symbol": inst.symbol,
                "name": inst.name,
                "type": inst.type.value,
                "quantity": float(pos.quantity),
                "current_value": float(pos.current_value) if pos.current_value else 0.0,
                "last_price": float(pos.last_price.last_price) if pos.last_price and pos.last_price.last_price else None,
                "pct_of_portfolio": float(pos.percent_of_portfolio) if pos.percent_of_portfolio else 0.0,
                "osi": inst.symbol if inst.type.value == "OPTION" else None,
            })
        bp = p.buying_power
        return {
            "positions": positions,
            "buying_power": float(bp.buying_power) if bp.buying_power else 0.0,
            "cash_only": float(bp.cash_only_buying_power) if bp.cash_only_buying_power else 0.0,
            "options_bp": float(bp.options_buying_power) if bp.options_buying_power else 0.0,
        }
    except Exception as e:
        print(f"Error fetching portfolio: {e}", file=sys.stderr)
        return {"positions": [], "buying_power": 0, "cash_only": 0, "options_bp": 0}


def fetch_underlying_for_position(pos: dict) -> str:
    """Extract underlying symbol from option OSI."""
    return underlying_from_position(pos)


def get_greeks_for_option(client, osi: str) -> dict:
    try:
        res = client.get_option_greeks(osi_symbols=[osi])
        if res and res.greeks:
            gv = res.greeks[0].greeks
            return greeks_to_dict(gv)
    except Exception as e:
        pass
    return {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}


def get_underlying_metrics(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        # 1y so the bootstrap VaR has ~250 joint return observations
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"]
        log_returns = np.log(closes / closes.shift(1)).dropna()
        rv_21 = float(log_returns.tail(21).std() * np.sqrt(252))
        info = t.info or {}
        return {
            "last": float(closes.dropna().iloc[-1]),
            "rv_21d": rv_21,
            "beta": info.get("beta", 1.0) or 1.0,
            "sector": info.get("sector", "Unknown"),
            "market_cap": info.get("marketCap", 0) or 0,
            "daily_returns": [float(r) for r in log_returns.values],
        }
    except Exception:
        return {}


def bootstrap_var(
    exposures: dict[str, dict],
    returns_map: dict[str, list[float]],
    num_samples: int = 10000,
    seed: int = 42,
    min_observations: int = 60,
) -> dict:
    """Empirical 1-day VaR from jointly resampled historical returns.

    `exposures` maps underlying -> {"delta_dollars": signed $ P&L per 100%
    move, "gamma_dollars": signed $ gamma term}. Whole historical days are
    sampled across all underlyings at once, so correlation and fat tails come
    from the data instead of the delta-normal beta*1.2% assumption (which the
    README itself warns "breaks in fat-tail events" — precisely the events a
    short-premium book is exposed to). P&L per sampled day:
        sum_u delta_dollars_u * r_u + 0.5 * gamma_dollars_u * r_u^2
    """
    symbols = [
        symbol
        for symbol in exposures
        if len(returns_map.get(symbol) or []) >= min_observations
    ]
    if not symbols:
        return {}
    depth = min(len(returns_map[symbol]) for symbol in symbols)
    # Tail-align on the most recent `depth` trading days across all symbols
    matrix = np.column_stack([np.asarray(returns_map[symbol][-depth:], dtype=float) for symbol in symbols])
    delta_vec = np.array([float(exposures[symbol].get("delta_dollars", 0.0)) for symbol in symbols])
    gamma_vec = np.array([float(exposures[symbol].get("gamma_dollars", 0.0)) for symbol in symbols])

    rng = np.random.default_rng(seed)
    sampled = matrix[rng.integers(0, depth, size=num_samples)]
    pnl = sampled @ delta_vec + 0.5 * (sampled**2) @ gamma_vec
    p5 = float(np.percentile(pnl, 5))
    tail = pnl[pnl <= p5]
    return {
        "var_1d_95": round(max(0.0, -p5), 2),
        "var_1d_99": round(max(0.0, -float(np.percentile(pnl, 1))), 2),
        "expected_shortfall_95": round(max(0.0, -float(tail.mean())) if len(tail) else 0.0, 2),
        "observations": depth,
        "num_samples": num_samples,
        "underlyings": symbols,
    }


def compute_risk(positions: list, metrics_map: dict) -> dict:
    """Compute portfolio-level risk metrics."""
    if not positions:
        return {}

    total_value = sum(p["current_value"] for p in positions)
    if total_value == 0:
        return {}

    # Aggregate Greeks (multiplied by contract multiplier 100, by 1 for equities)
    net_delta = 0.0
    net_gamma = 0.0
    net_theta = 0.0
    net_vega = 0.0

    # By underlying exposure — sum absolute notional exposure
    underlying_exposure = {}  # symbol -> {long, short, beta, value, sector}
    concentration = []  # (symbol, pct)
    # Signed dollar exposures per underlying for the bootstrap VaR
    bootstrap_exposures: dict[str, dict] = {}

    def exposure_bucket(und: str) -> dict:
        return bootstrap_exposures.setdefault(und, {"delta_dollars": 0.0, "gamma_dollars": 0.0})

    for pos in positions:
        und = fetch_underlying_for_position(pos)
        m = metrics_map.get(und, {})
        beta = m.get("beta", 1.0) or 1.0
        sector = m.get("sector", "Unknown")
        weight = pos["current_value"] / total_value
        spot = m.get("last", 0) or 0

        if pos["type"] == "OPTION":
            greeks = pos.get("greeks", {})
            qty = pos["quantity"]
            # Options multiplier 100, signed by long/short (positive qty = long, negative = short)
            sign = 1 if qty > 0 else -1
            net_delta += greeks.get("delta", 0) * abs(qty) * 100 * sign
            net_gamma += greeks.get("gamma", 0) * abs(qty) * 100 * sign
            net_theta += greeks.get("theta", 0) * abs(qty) * 100 * sign
            net_vega += greeks.get("vega", 0) * abs(qty) * 100 * sign

            # Underlying equivalent: delta * qty * 100 (in shares) * spot
            delta_shares = greeks.get("delta", 0) * abs(qty) * 100 * sign
            delta_value = delta_shares * spot
            bucket = exposure_bucket(und)
            bucket["delta_dollars"] += delta_value
            # P&L gamma term for return r: 0.5 * gamma * (spot*r)^2 * 100 * qty
            bucket["gamma_dollars"] += greeks.get("gamma", 0) * abs(qty) * 100 * sign * spot * spot
            if und not in underlying_exposure:
                underlying_exposure[und] = {"long": 0, "short": 0, "beta": beta, "sector": sector, "value": 0}
            if delta_value > 0:
                underlying_exposure[und]["long"] += delta_value
            else:
                underlying_exposure[und]["short"] += abs(delta_value)
            underlying_exposure[und]["value"] += pos["current_value"]
        else:
            # Equity
            qty = pos["quantity"]
            if und not in underlying_exposure:
                underlying_exposure[und] = {"long": 0, "short": 0, "beta": beta, "sector": sector, "value": 0}
            value = qty * spot
            exposure_bucket(und)["delta_dollars"] += value
            if value > 0:
                underlying_exposure[und]["long"] += value
            else:
                underlying_exposure[und]["short"] += abs(value)
            underlying_exposure[und]["value"] += pos["current_value"]

        concentration.append((und, weight))

    # Portfolio beta (weighted)
    port_beta = sum(c[1] * (metrics_map.get(c[0], {}).get("beta", 1.0) or 1.0) for c in concentration)

    # Sector concentration
    sector_exposure = {}
    for und, exp in underlying_exposure.items():
        s = exp["sector"]
        sector_exposure[s] = sector_exposure.get(s, 0) + exp["value"]
    sector_pcts = {s: v / total_value for s, v in sector_exposure.items()}

    # Concentration
    concentration.sort(key=lambda x: x[1], reverse=True)
    top_concentration = concentration[:5]

    # 1-day 95% VaR (delta-normal): VaR = 1.645 * portfolio_sigma * portfolio_value
    # Kept for continuity, but the bootstrap VaR below is the honest number —
    # delta-normal understates exactly the fat-tail events a short-premium
    # book is exposed to.
    market_sigma_1d = 0.012  # SPY daily vol ~1.2%
    port_sigma_1d = abs(port_beta) * market_sigma_1d
    var_1d_95 = 1.645 * port_sigma_1d * total_value

    # Empirical VaR: jointly resample the underlyings' actual return history
    returns_map = {
        und: metrics_map.get(und, {}).get("daily_returns") or []
        for und in bootstrap_exposures
    }
    empirical = bootstrap_var(bootstrap_exposures, returns_map)

    # Stress test
    stress_results = {}
    for shock in [-0.05, -0.10, -0.20]:
        # P&L = portfolio_beta * shock * portfolio_value (equity approximation)
        pnl = port_beta * shock * total_value
        stress_results[f"{shock*100:+.0f}%"] = pnl

    return {
        "total_value": total_value,
        "net_delta_shares": net_delta,
        "net_gamma": net_gamma,
        "net_theta_per_day": net_theta,
        "net_vega_per_1pct_iv": net_vega,
        "portfolio_beta": port_beta,
        "sector_concentration": sector_pcts,
        "top_holdings": top_concentration,
        "var_1d_95": var_1d_95,
        "var_bootstrap": empirical,
        "stress_test": stress_results,
    }


def print_dashboard(portfolio: dict, risk: dict, demo: bool = False):
    print(f"\n{'#'*78}")
    print(f"# PORTFOLIO RISK DASHBOARD — {datetime.now().isoformat()}")
    if demo:
        print(f"# (DEMO MODE — simulated positions)")
    print(f"{'#'*78}\n")

    bp = portfolio.get("buying_power", 0)
    cash = portfolio.get("cash_only", 0)
    obp = portfolio.get("options_bp", 0)
    positions = portfolio.get("positions", [])

    print(f"  Buying Power:        ${bp:>12,.2f}")
    print(f"  Cash Only:           ${cash:>12,.2f}")
    print(f"  Options BP:          ${obp:>12,.2f}")
    print(f"  Open Positions:      {len(positions)}")

    if not positions:
        print(f"\n  → No open positions. Nothing to risk-analyze.")
        return

    if not risk:
        print(f"\n  → Positions exist but risk metrics unavailable.")
        return

    print(f"\n{'─'*78}\n  AGGREGATE GREEKS\n{'─'*78}")
    print(f"  Net Delta (share-equivalent):  {risk['net_delta_shares']:>+10.1f} shares")
    print(f"  Net Gamma:                     {risk['net_gamma']:>+10.4f}")
    print(f"  Net Theta (per day):           ${risk['net_theta_per_day']:>+9.2f}")
    print(f"  Net Vega (per 1% IV):          ${risk['net_vega_per_1pct_iv']:>+9.2f}")

    print(f"\n{'─'*78}\n  MARKET EXPOSURE\n{'─'*78}")
    print(f"  Portfolio Beta:                {risk['portfolio_beta']:>10.3f}")
    print(f"  1-day 95% VaR (delta-normal):  ${risk['var_1d_95']:>11,.2f}  "
          f"({risk['var_1d_95']/risk['total_value']*100:.2f}% of NAV)")
    empirical = risk.get("var_bootstrap") or {}
    if empirical:
        print(f"  1-day 95% VaR (bootstrap):     ${empirical['var_1d_95']:>11,.2f}  "
              f"({empirical['var_1d_95']/risk['total_value']*100:.2f}% of NAV)")
        print(f"  1-day 99% VaR (bootstrap):     ${empirical['var_1d_99']:>11,.2f}")
        print(f"  95% expected shortfall:        ${empirical['expected_shortfall_95']:>11,.2f}  "
              f"({empirical['observations']} joint return days)")

    print(f"\n{'─'*78}\n  STRESS TESTS\n{'─'*78}")
    print(f"  {'Scenario':<10}  {'Est. P&L':>12}  {'% of NAV':>10}")
    for scen, pnl in risk["stress_test"].items():
        pct = pnl / risk["total_value"] * 100
        sign = "+" if pnl >= 0 else ""
        print(f"  {scen:<10}  {sign}${pnl:>10,.2f}  {sign}{pct:>8.2f}%")

    print(f"\n{'─'*78}\n  TOP HOLDINGS (by weight)\n{'─'*78}")
    for sym, w in risk["top_holdings"][:5]:
        print(f"  {sym:<8}  {w*100:>6.2f}%")

    print(f"\n{'─'*78}\n  SECTOR EXPOSURE\n{'─'*78}")
    for s, p in sorted(risk["sector_concentration"].items(), key=lambda x: -x[1]):
        bar = "█" * int(p * 30)
        print(f"  {s:<20} {p*100:>5.1f}%  {bar}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target-watchlist", nargs="+",
                    help="If account is empty, demo with this watchlist as 100-share lots")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_client()
    portfolio = fetch_portfolio(client)

    if not portfolio["positions"] and args.target_watchlist:
        # Demo mode: simulate owning 100 shares of each watchlist ticker
        print("Empty account — using demo mode with target watchlist", file=sys.stderr)
        portfolio["positions"] = []
        metrics_map = {}
        for sym in args.target_watchlist:
            m = get_underlying_metrics(sym)
            metrics_map[sym] = m
            if m.get("last"):
                portfolio["positions"].append({
                    "symbol": sym,
                    "name": sym,
                    "type": "EQUITY",
                    "quantity": 100,
                    "current_value": 100 * m["last"],
                    "last_price": m["last"],
                    "pct_of_portfolio": 0,
                    "osi": None,
                })
        # Recompute weights
        total = sum(p["current_value"] for p in portfolio["positions"])
        for p in portfolio["positions"]:
            p["pct_of_portfolio"] = (p["current_value"] / total) * 100
        risk = compute_risk(portfolio["positions"], metrics_map)
        if args.json:
            print(json.dumps({"portfolio": portfolio, "risk": risk, "demo": True}, indent=2, default=str))
            return
        print_dashboard(portfolio, risk, demo=True)
        return

    # Real portfolio: enrich with greeks for any options
    metrics_map = {}
    osis_to_fetch = []
    pos_lookup = {}
    for pos in portfolio["positions"]:
        und = fetch_underlying_for_position(pos)
        if und not in metrics_map:
            metrics_map[und] = get_underlying_metrics(und)
        pos["underlying_symbol"] = und
        pos["underlying_price"] = metrics_map[und].get("last")
        if pos["type"] == "OPTION":
            osis_to_fetch.append(pos["symbol"])
            pos_lookup[pos["symbol"]] = pos

    # Batch fetch greeks
    if osis_to_fetch:
        try:
            res = client.get_option_greeks(osi_symbols=osis_to_fetch)
            if res and res.greeks:
                for g in res.greeks:
                    osi = g.symbol
                    gv = g.greeks
                    if osi in pos_lookup:
                        pos_lookup[osi]["greeks"] = greeks_to_dict(gv)
        except Exception as e:
            print(f"Greeks batch failed: {e}", file=sys.stderr)

    # For positions without greeks, give empty dict
    for pos in portfolio["positions"]:
        if pos["type"] == "OPTION" and "greeks" not in pos:
            pos["greeks"] = {"delta": 0, "gamma": 0, "theta": 0, "vega": 0, "iv": 0}

    risk = compute_risk(portfolio["positions"], metrics_map)
    if args.json:
        print(json.dumps({"portfolio": portfolio, "risk": risk, "demo": False}, indent=2, default=str))
        return

    print_dashboard(portfolio, risk, demo=False)


if __name__ == "__main__":
    main()
