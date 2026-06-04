#!/usr/bin/env python3.12
"""
options_screener.py — Live options screener using Public.com + yfinance data.

Scans a watchlist for high-probability options setups:
  1. Cash-Secured Puts (CSP) — annualized yield on strike, delta-filtered
  2. Covered Calls (CC) — annualized yield on market value, delta-filtered
  3. Bull Put Spreads — credit / max-loss ratio, POP estimate

Data sources (all live, no mocks):
  - Live bid/ask/last/volume/OI from Public.com chain endpoint
  - Live Greeks (delta, gamma, theta, vega, IV) from Public.com greeks endpoint
  - Spot price + realized vol + earnings dates from yfinance

Usage:
  ./options_screener.py --watchlist SPY QQQ NVDA AAPL MSFT --strategies csp cc
  ./options_screener.py --watchlist SPY --strategies csp bull_put --min-dte 21 --max-dte 45
  ./options_screener.py --watchlist NVDA --strategies csp --target-delta 0.20
"""
import argparse
import json
import os
import sys
import math
import time
from datetime import datetime, date, timedelta
from pathlib import Path

SCRIPTS_DIR = Path("/home/chavez_f/.hermes/skills/openclaw-imports/public-dot-com/scripts")
sys.path.insert(0, str(SCRIPTS_DIR))
from config import get_api_secret, get_account_id

from public_api_sdk import (
    PublicApiClient, PublicApiClientConfiguration,
    OrderInstrument, InstrumentType, OptionChainRequest,
)
from public_api_sdk.auth_config import ApiKeyAuthConfig

import yfinance as yf
import numpy as np


# ---------------------------------------------------------------------------
# Public.com helpers
# ---------------------------------------------------------------------------

def get_client():
    secret = get_api_secret()
    if not secret:
        print("Error: PUBLIC_COM_SECRET missing.", file=sys.stderr)
        sys.exit(1)
    return PublicApiClient(
        ApiKeyAuthConfig(api_secret_key=secret),
        config=PublicApiClientConfiguration(default_account_number=get_account_id() or ""),
    )


def fetch_quote(client, symbol: str) -> dict:
    try:
        res = client.get_quotes(instruments=[{"symbol": symbol, "type": "EQUITY"}])
        if res:
            q = res[0]
            return {
                "last": float(q.last) if q.last else None,
                "bid": float(q.bid) if q.bid else None,
                "ask": float(q.ask) if q.ask else None,
                "prev_close": float(q.previous_close) if hasattr(q, 'previous_close') and q.previous_close else None,
                "volume": int(q.volume) if hasattr(q, 'volume') and q.volume else 0,
            }
    except Exception as e:
        print(f"  ! quote failed: {e}", file=sys.stderr)
    return {}


def fetch_option_expirations(client, symbol: str) -> list[str]:
    """Use the dedicated expirations endpoint. Returns list of YYYY-MM-DD strings."""
    from public_api_sdk import OptionExpirationsRequest
    try:
        req = OptionExpirationsRequest(instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY))
        res = client.get_option_expirations(req)
        if res and hasattr(res, 'expirations') and res.expirations:
            return [str(e) for e in res.expirations if e]
    except Exception as e:
        print(f"  ! expirations failed: {e}", file=sys.stderr)
    return []


def parse_osi_strike(osi: str) -> float:
    try:
        return int(osi[-8:]) / 1000.0
    except (ValueError, IndexError):
        return None


def fetch_chain_with_greeks(client, symbol: str, expiration: str, spot: float,
                            max_legs: int = 50) -> dict:
    """
    Pull the chain and selectively fetch Greeks for strikes within
    ±10% of spot. Returns dict {'calls': {strike: {...}}, 'puts': {...}}.
    """
    try:
        ch = client.get_option_chain(OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            expiration_date=expiration,
        ))
    except Exception as e:
        print(f"  ! chain failed: {e}", file=sys.stderr)
        return {"calls": {}, "puts": {}}

    calls = ch.calls or []
    puts = ch.puts or []

    # Find ATM strikes — collect OSI symbols within ±15% of spot, sorted by delta-proximity to 0.5
    # We want a mix of strikes so we can build spreads later
    band_low, band_high = spot * 0.85, spot * 1.15
    atm_legs = []
    for leg in calls + puts:
        if not hasattr(leg, 'instrument') or not leg.instrument:
            continue
        osi = leg.instrument.symbol
        if not osi:
            continue
        strike = parse_osi_strike(osi)
        if strike and band_low <= strike <= band_high:
            # Distance from 0.5 delta (ATM proxy) — but we want a wide range for spread legs
            atm_legs.append((abs(strike - spot), osi))
    atm_legs.sort()
    # Spread the selection: take nearest 20 + a sample of farther OTM
    nearest = [osi for _, osi in atm_legs[:max_legs]]
    farther = [osi for _, osi in atm_legs[max_legs:max_legs*2]]
    atm_osis = nearest + farther

    # Batch-fetch greeks
    greeks_map = {}
    if atm_osis:
        # Cap to avoid massive requests
        atm_osis = atm_osis[:max_legs * 2]
        try:
            greeks_res = client.get_option_greeks(osi_symbols=atm_osis)
            if greeks_res and hasattr(greeks_res, 'greeks') and greeks_res.greeks:
                for g in greeks_res.greeks:
                    osi = getattr(g, 'symbol', None)
                    if not osi:
                        continue
                    gv = getattr(g, 'greeks', None)
                    if not gv:
                        continue
                    greeks_map[osi] = {
                        "delta": float(gv.delta) if gv.delta is not None else None,
                        "gamma": float(gv.gamma) if gv.gamma is not None else None,
                        "theta": float(gv.theta) if gv.theta is not None else None,
                        "vega": float(gv.vega) if gv.vega is not None else None,
                        "rho": float(gv.rho) if gv.rho is not None else None,
                        "iv": float(gv.implied_volatility) if gv.implied_volatility is not None else None,
                    }
        except Exception as e:
            print(f"  ! greeks batch failed (will proceed without): {e}", file=sys.stderr)

    def build_legs(legs, side):
        out = {}
        for leg in legs:
            if not hasattr(leg, 'instrument') or not leg.instrument:
                continue
            osi = leg.instrument.symbol
            if not osi:
                continue
            strike = parse_osi_strike(osi)
            if not strike or not (band_low <= strike <= band_high):
                continue
            bid = float(leg.bid) if leg.bid else 0.0
            ask = float(leg.ask) if leg.ask else 0.0
            last = float(leg.last) if leg.last else 0.0
            mark = (bid + ask) / 2 if (bid and ask) else last
            g = greeks_map.get(osi, {})
            out[strike] = {
                "bid": bid,
                "ask": ask,
                "last": last,
                "mark": mark,
                "volume": int(leg.volume) if leg.volume else 0,
                "open_interest": int(leg.open_interest) if leg.open_interest else 0,
                "osi": osi,
                "side": side,
                **{k: v for k, v in g.items() if v is not None},
            }
        return out

    return {"calls": build_legs(calls, "call"), "puts": build_legs(puts, "put")}


# ---------------------------------------------------------------------------
# yfinance helpers
# ---------------------------------------------------------------------------

def fetch_underlying_metrics(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="6mo", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"]
        log_returns = np.log(closes / closes.shift(1)).dropna()
        rv_21 = float(log_returns.tail(21).std() * np.sqrt(252))
        rv_60 = float(log_returns.tail(60).std() * np.sqrt(252))
        info = t.info or {}
        return {
            "last_close": float(closes.iloc[-1]),
            "rv_21d_pct": rv_21 * 100,
            "rv_60d_pct": rv_60 * 100,
            "iv_rank_proxy_pct": min(100, max(0, (rv_21 / max(0.05, 0.30)) * 100)),  # crude
            "beta": info.get("beta"),
            "week_52_high": info.get("fiftyTwoWeekHigh"),
            "week_52_low": info.get("fiftyTwoWeekLow"),
            "market_cap": info.get("marketCap"),
            "sector": info.get("sector"),
            "dividend_yield": info.get("dividendYield"),
            "earnings": _next_earnings(t),
        }
    except Exception as e:
        print(f"  ! yfinance failed: {e}", file=sys.stderr)
        return {}


def _next_earnings(t: yf.Ticker) -> dict:
    try:
        cal = t.calendar
        if cal is None:
            return {}
        if hasattr(cal, 'empty') and cal.empty:
            return {}
        if isinstance(cal, dict) and "Earnings Date" in cal:
            ed = cal["Earnings Date"]
            if hasattr(ed, '__iter__') and not isinstance(ed, str):
                ed = list(ed)[0]
            return {"next": str(ed)[:10]}
        if hasattr(cal, 'columns') and "Earnings Date" in cal.columns:
            ed = cal["Earnings Date"].iloc[0]
            return {"next": str(ed)[:10]}
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Strategy screens
# ---------------------------------------------------------------------------

def annualized_roc(credit: float, capital: float, dte: int) -> float:
    if capital <= 0 or dte <= 0:
        return 0.0
    return (credit / capital) * (365 / dte)


def screen_csp(chain: dict, spot: float, dte: int, target_delta: float = -0.30,
               min_oi: int = 50, min_volume: int = 0) -> list[dict]:
    puts = chain.get("puts", {})
    candidates = []
    for strike, leg in puts.items():
        if leg.get("delta") is None:
            continue
        if abs(leg["delta"] - target_delta) > 0.10:
            continue
        if leg["open_interest"] < min_oi:
            continue
        if leg["volume"] < min_volume:
            continue
        if leg["mark"] <= 0.05:
            continue
        # Strike must be below spot for a put you want assigned-or-expire
        if strike >= spot * 1.005:
            continue
        credit = leg["mark"]
        capital = strike * 100
        delta = leg["delta"]
        candidates.append({
            "strategy": "CSP",
            "strike": strike,
            "dte": dte,
            "credit": credit,
            "bid": leg["bid"],
            "ask": leg["ask"],
            "capital": capital,
            "delta": delta,
            "iv_pct": leg.get("iv", 0) * 100 if leg.get("iv") else None,
            "theta": leg.get("theta"),
            "pop_pct": (1.0 + delta) * 100,  # P(expire OTM)
            "ann_roc_pct": annualized_roc(credit, capital, dte) * 100,
            "breakeven": strike - credit,
            "distance_to_strike_pct": ((spot - strike) / spot) * 100,
            "volume": leg["volume"],
            "open_interest": leg["open_interest"],
            "osi": leg["osi"],
        })
    candidates.sort(key=lambda r: r["ann_roc_pct"], reverse=True)
    return candidates[:5]


def screen_cc(chain: dict, spot: float, dte: int, target_delta: float = 0.30,
              min_oi: int = 50) -> list[dict]:
    calls = chain.get("calls", {})
    candidates = []
    for strike, leg in calls.items():
        if leg.get("delta") is None:
            continue
        if abs(leg["delta"] - target_delta) > 0.10:
            continue
        if leg["open_interest"] < min_oi:
            continue
        if strike < spot * 0.99:  # skip deep ITM
            continue
        if leg["mark"] <= 0.05:
            continue
        credit = leg["mark"]
        capital = spot * 100  # cost basis proxy
        delta = leg["delta"]
        candidates.append({
            "strategy": "CC",
            "strike": strike,
            "dte": dte,
            "credit": credit,
            "bid": leg["bid"],
            "ask": leg["ask"],
            "capital": capital,
            "delta": delta,
            "iv_pct": leg.get("iv", 0) * 100 if leg.get("iv") else None,
            "theta": leg.get("theta"),
            "pop_pct": (1.0 - delta) * 100,  # P(expire OTM)
            "ann_roc_pct": annualized_roc(credit, capital, dte) * 100,
            "breakeven": spot - credit,  # if you buy at spot and sell call
            "distance_to_strike_pct": ((strike - spot) / spot) * 100,
            "volume": leg["volume"],
            "open_interest": leg["open_interest"],
            "osi": leg["osi"],
        })
    candidates.sort(key=lambda r: r["ann_roc_pct"], reverse=True)
    return candidates[:5]


def screen_bull_put(chain: dict, spot: float, dte: int,
                    short_delta: float = -0.20, wing_width: float = 5.0,
                    min_oi: int = 50) -> list[dict]:
    puts = chain.get("puts", {})
    candidates = []
    for strike, short_leg in puts.items():
        if short_leg.get("delta") is None:
            continue
        if abs(short_leg["delta"] - short_delta) > 0.08:
            continue
        if short_leg["open_interest"] < min_oi:
            continue
        # Try integer / 0.5 / 1.0 strikes
        for delta in (0, 0.5, 1.0):
            long_strike = round((strike - wing_width - delta) * 2) / 2
            long_leg = puts.get(long_strike)
            if long_leg and long_leg.get("mark") and long_leg["mark"] > 0:
                break
        if not long_leg or not long_leg.get("mark"):
            continue
        if short_leg["mark"] <= 0.10 or long_leg["mark"] <= 0:
            continue
        credit = short_leg["mark"] - long_leg["mark"]
        if credit <= 0.05:
            continue
        max_loss_per_share = wing_width - credit
        if max_loss_per_share <= 0:
            continue
        max_loss = max_loss_per_share * 100
        ratio = (credit * 100) / max_loss if max_loss > 0 else 0
        candidates.append({
            "strategy": "BULL_PUT",
            "short_strike": strike,
            "long_strike": long_strike,
            "dte": dte,
            "credit": credit,
            "max_loss": max_loss,
            "ratio": ratio,
            "pop_pct": (1.0 + short_leg["delta"]) * 100,
            "delta_short": short_leg["delta"],
            "iv_short_pct": short_leg.get("iv", 0) * 100 if short_leg.get("iv") else None,
            "ann_roc_pct": (credit / (wing_width * 100)) * (365 / dte) * 100,
            "volume_short": short_leg["volume"],
            "open_interest_short": short_leg["open_interest"],
        })
    candidates.sort(key=lambda r: r["ratio"], reverse=True)
    return candidates[:5]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", nargs="+", required=True)
    ap.add_argument("--strategies", nargs="+", default=["csp", "cc"],
                    choices=["csp", "cc", "bull_put"])
    ap.add_argument("--min-dte", type=int, default=14)
    ap.add_argument("--max-dte", type=int, default=45)
    ap.add_argument("--target-delta", type=float, default=0.30)
    ap.add_argument("--min-oi", type=int, default=50)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--report", type=str)
    args = ap.parse_args()

    client = get_client()
    all_results = {
        "as_of": datetime.now().isoformat(),
        "params": vars(args),
        "tickers": {},
    }

    for symbol in args.watchlist:
        symbol = symbol.upper()
        print(f"\n--- {symbol} ---", file=sys.stderr)
        quote = fetch_quote(client, symbol)
        spot = quote.get("last") or quote.get("bid")
        if not spot:
            metrics = fetch_underlying_metrics(symbol)
            spot = metrics.get("last_close")
        if not spot:
            print(f"  ! no spot price, skipping", file=sys.stderr)
            continue

        metrics = fetch_underlying_metrics(symbol)
        rv = metrics.get("rv_21d_pct", 0)
        iv_rank = metrics.get("iv_rank_proxy_pct", 0)
        print(f"  spot=${spot:.2f}  RV21d={rv:.1f}%  IV-rank-prox={iv_rank:.0f}  "
              f"earnings={metrics.get('earnings', {}).get('next', 'n/a')}",
              file=sys.stderr)

        expirations = fetch_option_expirations(client, symbol)
        if not expirations:
            continue
        target_dte = (args.min_dte + args.max_dte) // 2
        best_exp, best_dte, best_diff = None, None, 9999
        for exp in expirations:
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            except ValueError:
                continue
            dte = (exp_date - date.today()).days
            if dte < args.min_dte or dte > args.max_dte:
                continue
            if abs(dte - target_dte) < best_diff:
                best_exp, best_dte, best_diff = exp, dte, abs(dte - target_dte)
        if not best_exp:
            print(f"  ! no expiration in DTE {args.min_dte}-{args.max_dte}", file=sys.stderr)
            continue
        print(f"  expiration={best_exp}  DTE={best_dte}", file=sys.stderr)

        chain = fetch_chain_with_greeks(client, symbol, best_exp, spot)
        if not chain["calls"] and not chain["puts"]:
            print(f"  ! empty chain", file=sys.stderr)
            continue
        n_greeks = sum(1 for leg in chain["calls"].values() if "delta" in leg) + \
                   sum(1 for leg in chain["puts"].values() if "delta" in leg)
        print(f"  strikes: {len(chain['calls'])} calls, {len(chain['puts'])} puts "
              f"({n_greeks} with greeks)", file=sys.stderr)

        ticker_results = {
            "spot": spot,
            "metrics": metrics,
            "expiration": best_exp,
            "dte": best_dte,
            "strategies": {},
        }
        target_d = -args.target_delta if args.strategies and "csp" in args.strategies else args.target_delta
        for strat in args.strategies:
            if strat == "csp":
                res = screen_csp(chain, spot, best_dte, target_delta=-args.target_delta, min_oi=args.min_oi)
            elif strat == "cc":
                res = screen_cc(chain, spot, best_dte, target_delta=args.target_delta, min_oi=args.min_oi)
            elif strat == "bull_put":
                res = screen_bull_put(chain, spot, best_dte, short_delta=-args.target_delta, min_oi=args.min_oi)
            else:
                res = []
            ticker_results["strategies"][strat] = res
        all_results["tickers"][symbol] = ticker_results

    if args.json:
        print(json.dumps(all_results, indent=2, default=str))
    else:
        print_report(all_results)

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "w") as f:
            json.dump(all_results, f, indent=2, default=str)
        print(f"\nReport: {args.report}", file=sys.stderr)


def print_report(results: dict):
    print(f"\n\n{'#'*78}")
    print(f"# OPTIONS SCREENER — {results['as_of']}")
    print(f"# Strategies: {', '.join(results['params']['strategies'])}  "
          f"DTE: {results['params']['min_dte']}-{results['params']['max_dte']}  "
          f"target-Δ: {results['params']['target_delta']}")
    print(f"{'#'*78}\n")

    for ticker, data in results["tickers"].items():
        print(f"\n{'='*78}")
        print(f"  {ticker}  spot=${data['spot']:.2f}  exp={data['expiration']}  DTE={data['dte']}")
        m = data.get("metrics", {})
        if m.get("rv_21d_pct"):
            print(f"  RV 21d={m['rv_21d_pct']:.1f}%  RV 60d={m['rv_60d_pct']:.1f}%  "
                  f"beta={m.get('beta', 'N/A')}  IV-rank-prox={m.get('iv_rank_proxy_pct', 0):.0f}")
        if m.get("earnings", {}).get("next"):
            print(f"  Next earnings: {m['earnings']['next']}")
        print(f"{'='*78}")

        for strat_name, rows in data["strategies"].items():
            if not rows:
                print(f"\n  {strat_name.upper()}: no candidates matching filters")
                continue
            print(f"\n  ▸ {strat_name.upper()} — top {len(rows)}")
            if strat_name in ("csp", "cc"):
                print(f"  {'Strike':>8} {'Credit':>8} {'Δ':>7} {'POP':>6} {'AnnROC':>7} {'IV%':>6} {'Θ':>7} {'Vol':>5} {'OI':>6}  OSI")
                for r in rows:
                    iv = r.get("iv_pct")
                    iv_s = f"{iv:>5.1f}%" if iv is not None else "  -- "
                    th = r.get("theta")
                    th_s = f"{th:>7.3f}" if th is not None else "    -- "
                    print(f"  ${r['strike']:>7.2f} ${r['credit']:>7.2f} {r['delta']:>7.3f} "
                          f"{r['pop_pct']:>5.1f}% {r['ann_roc_pct']:>6.2f}% {iv_s} {th_s} "
                          f"{r['volume']:>5} {r['open_interest']:>6}  {r['osi']}")
            elif strat_name == "bull_put":
                print(f"  {'Short':>7} {'Long':>7} {'Credit':>8} {'MaxLoss':>9} {'Ratio':>6} {'POP':>6} {'AnnROC':>7}")
                for r in rows:
                    print(f"  ${r['short_strike']:>6.2f} ${r['long_strike']:>6.2f} "
                          f"${r['credit']:>7.2f} ${r['max_loss']:>8.2f} "
                          f"{r['ratio']:>5.2f} {r['pop_pct']:>5.1f}% {r['ann_roc_pct']:>6.2f}%")


if __name__ == "__main__":
    main()
