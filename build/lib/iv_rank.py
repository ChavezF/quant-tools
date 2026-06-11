#!/usr/bin/env python3.12
"""
iv_rank.py — Real IV rank & IV percentile for tickers.

For each ticker:
  1. Pull 1 year of daily closes from yfinance
  2. Compute trailing 30d realized vol for each day in that year (annualized)
  3. Pull current ATM 30d implied vol from Public.com (live)
  4. Compute:
     - IV rank = (current_iv - 52w_low) / (52w_high - 52w_low) * 100
     - IV percentile = % of days current_iv exceeded historical 30d RV
     - HV rank / percentile (same, using current 30d RV vs history)

This is the tastytrade-style IV rank, except we use 30d RV as the historical
benchmark instead of historical IV (cleaner — we don't need to fetch a year
of option chains).

Usage:
  ./iv_rank.py --tickers SPY QQQ NVDA AAPL MSFT TSLA AMD
  ./iv_rank.py --tickers SPY --json
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
import numpy as np
import yfinance as yf

from common import configure_public_imports, get_public_client, parse_osi_strike

configure_public_imports()

from public_api_sdk import (
    OrderInstrument, InstrumentType, OptionChainRequest, OptionExpirationsRequest,
)


def get_client():
    return get_public_client()


def get_current_iv(client, symbol: str, spot: float) -> float:
    """Get current 30d ATM IV from the closest expiration to 30 DTE."""
    from datetime import date as _date
    try:
        exps = client.get_option_expirations(OptionExpirationsRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY)
        ))
        exps = [str(e) for e in (exps.expirations or [])]
        if not exps:
            return 0.0
        # Pick expiration closest to 30 DTE
        target_dte = 30
        best_exp, best_diff = None, 9999
        for e in exps:
            try:
                d = datetime.strptime(e, "%Y-%m-%d").date()
                dte = (d - _date.today()).days
            except ValueError:
                continue
            if 14 <= dte <= 60 and abs(dte - target_dte) < best_diff:
                best_exp, best_diff = e, abs(dte - target_dte)
        if not best_exp:
            return 0.0
        dte = (datetime.strptime(best_exp, "%Y-%m-%d").date() - _date.today()).days

        ch = client.get_option_chain(OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            expiration_date=best_exp,
        ))
        calls = ch.calls or []
        puts = ch.puts or []
        if not calls or not puts:
            return 0.0
        # Find ATM call and put
        atm_call = min(calls, key=lambda c: abs((parse_osi_strike(c.instrument.symbol) or 0.0) - spot), default=None)
        atm_put = min(puts, key=lambda p: abs((parse_osi_strike(p.instrument.symbol) or 0.0) - spot), default=None)
        if not atm_call or not atm_put:
            return 0.0
        cb = float(atm_call.bid) if atm_call.bid else 0
        ca = float(atm_call.ask) if atm_call.ask else 0
        pb = float(atm_put.bid) if atm_put.bid else 0
        pa = float(atm_put.ask) if atm_put.ask else 0
        call_mark = (cb + ca) / 2 if cb and ca else float(atm_call.last or 0)
        put_mark = (pb + pa) / 2 if pb and pa else float(atm_put.last or 0)
        straddle = call_mark + put_mark
        # 1-sigma IV ≈ straddle / spot / sqrt(dte/365) * sqrt(252) ... use simpler form
        # 1-sigma move = straddle/spot.  Implied annualized vol = (straddle/spot) / sqrt(dte/365)
        emove = straddle / spot
        if dte <= 0:
            return 0.0
        iv_annual = emove / np.sqrt(dte / 365.0)
        return iv_annual * 100  # as percentage
    except Exception as e:
        print(f"  ! {symbol} IV fetch failed: {e}", file=sys.stderr)
        return 0.0


def get_historical_rv_series(symbol: str, lookback_days: int = 365) -> list[float]:
    """Compute 30d realized vol (annualized %) for each trading day in lookback."""
    try:
        t = yf.Ticker(symbol)
        # Need ~1y + 30d of data
        end = datetime.now()
        start = end - timedelta(days=lookback_days + 60)
        hist = t.history(start=start, end=end, auto_adjust=True)
        if hist.empty or len(hist) < 60:
            return []
        closes = hist["Close"]
        log_returns = np.log(closes / closes.shift(1)).dropna()
        # 30d rolling RV
        rv_series = (log_returns.rolling(30).std() * np.sqrt(252) * 100).dropna()
        return rv_series.tolist()
    except Exception as e:
        print(f"  ! {symbol} history failed: {e}", file=sys.stderr)
        return []


def rank_metrics(current: float, history: list[float]) -> dict:
    """Compute IV rank and IV percentile against historical 30d RV series."""
    if not history or current <= 0:
        return {"rank": None, "percentile": None, "low_52w": None, "high_52w": None, "mean": None, "median": None}
    arr = np.array(history)
    low = float(np.min(arr))
    high = float(np.max(arr))
    mean = float(np.mean(arr))
    median = float(np.median(arr))
    # IV rank: where does current fall in the [low, high] range?
    if high > low:
        rank = ((current - low) / (high - low)) * 100
        rank = max(0.0, min(100.0, rank))
    else:
        rank = 50.0
    # IV percentile: % of historical days current exceeded
    percentile = float((arr < current).sum() / len(arr) * 100)
    return {
        "rank": round(rank, 1),
        "percentile": round(percentile, 1),
        "low_52w": round(low, 1),
        "high_52w": round(high, 1),
        "mean": round(mean, 1),
        "median": round(median, 1),
    }


def classify_iv_regime(rank: float | None) -> str:
    """Tastytrade-style IV regime classification."""
    if rank is None:
        return "?"
    if rank < 25:
        return "low (buy premium)"
    if rank < 50:
        return "below-median (cautious sell)"
    if rank < 75:
        return "above-median (sell premium)"
    return "high (aggressive sell)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_client()
    results = {"as_of": datetime.now().isoformat(), "tickers": {}}

    for sym in args.tickers:
        sym = sym.upper()
        print(f"  {sym}: pulling 1y RV history + current IV...", file=sys.stderr)

        # Spot
        try:
            t = yf.Ticker(sym)
            hist = t.history(period="5d", auto_adjust=True)
            spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0
        except Exception:
            spot = 0
        if not spot:
            print(f"  ! no spot for {sym}", file=sys.stderr)
            continue

        # Current IV
        current_iv = get_current_iv(client, sym, spot)
        if current_iv <= 0:
            print(f"  ! no IV for {sym}", file=sys.stderr)
            continue

        # Historical RV series
        rv_series = get_historical_rv_series(sym)
        if not rv_series:
            print(f"  ! no RV history for {sym}", file=sys.stderr)
            continue

        # Current 30d RV (last 30 days of log returns std)
        closes = t.history(period="6mo", auto_adjust=True)["Close"]
        if len(closes) < 30:
            continue
        log_returns = np.log(closes / closes.shift(1)).dropna()
        current_rv = float(log_returns.tail(30).std() * np.sqrt(252) * 100)

        # Compute ranks
        iv_metrics = rank_metrics(current_iv, rv_series)
        rv_metrics = rank_metrics(current_rv, rv_series)

        results["tickers"][sym] = {
            "spot": spot,
            "current_iv_30d_pct": round(current_iv, 2),
            "current_rv_30d_pct": round(current_rv, 2),
            "iv_minus_rv": round(current_iv - current_rv, 2),
            "iv_rank": iv_metrics["rank"],
            "iv_percentile": iv_metrics["percentile"],
            "iv_52w_low": iv_metrics["low_52w"],
            "iv_52w_high": iv_metrics["high_52w"],
            "iv_52w_mean": iv_metrics["mean"],
            "iv_regime": classify_iv_regime(iv_metrics["rank"]),
            "hv_rank": rv_metrics["rank"],
            "hv_percentile": rv_metrics["percentile"],
        }

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    # Pretty print
    print(f"\n\n{'#'*78}")
    print(f"# IV RANK & PERCENTILE — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'#'*78}\n")

    print(f"  {'Ticker':<6} {'Spot':>8} {'IV%':>6} {'RV%':>6} {'IV-RV':>6} "
          f"{'IVRank':>6} {'IV%ile':>6} {'52wLo':>6} {'52wHi':>6}  Regime")
    print(f"  {'-'*6} {'-'*8} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6} {'-'*6}  ------")
    for sym, r in results["tickers"].items():
        print(f"  {sym:<6} ${r['spot']:>7.2f} {r['current_iv_30d_pct']:>5.1f}% {r['current_rv_30d_pct']:>5.1f}% "
              f"{r['iv_minus_rv']:>+5.1f}% {r['iv_rank']:>5.1f} {r['iv_percentile']:>5.1f} "
              f"{r['iv_52w_low']:>5.1f} {r['iv_52w_high']:>5.1f}  {r['iv_regime']}")

    print("\n  Reading: IVRank < 25 = cheap premium (buy vol / wait).")
    print("          IVRank 25-50 = below median (cautious short premium, smaller size).")
    print("          IVRank 50-75 = above median (sweet spot for short premium).")
    print("          IVRank > 75 = rich premium (aggressive short premium / earnings plays).")


if __name__ == "__main__":
    main()
