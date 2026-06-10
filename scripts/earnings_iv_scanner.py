#!/usr/bin/env python3.12
"""
earnings_iv_scanner.py — Find earnings plays with elevated IV.

For each ticker in the watchlist:
  - Pull next earnings date
  - Pull 21d realized vol
  - Pull current ATM IV from the nearest expiration *after* earnings
  - Compute IV-vs-RV spread (high spread = vol crush opportunity)
  - Compute expected move (straddle price from ATM straddle)

Output: ranked list of upcoming earnings with rich-premium setups.

Usage:
  ./earnings_iv_scanner.py --watchlist NVDA AAPL MSFT TSLA AMZN META GOOGL
  ./earnings_iv_scanner.py --watchlist AAPL --days-ahead 14
"""
import argparse
import json
import sys
from datetime import datetime, date, timedelta
import numpy as np
import yfinance as yf

from common import configure_public_imports, get_public_client as get_client, parse_osi_parts

configure_public_imports()

from public_api_sdk import (
    OrderInstrument, InstrumentType, OptionChainRequest, OptionExpirationsRequest,
)


def parse_osi_strike(osi: str) -> float:
    return parse_osi_parts(osi).get("strike") or 0.0


def get_earnings_and_rv(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period="6mo", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"]
        log_returns = np.log(closes / closes.shift(1)).dropna()
        rv_21 = float(log_returns.tail(21).std() * np.sqrt(252))
        rv_60 = float(log_returns.tail(60).std() * np.sqrt(252))

        # Earnings
        earnings_date = None
        try:
            cal = t.calendar
            if cal is not None and not (hasattr(cal, "empty") and cal.empty):
                if isinstance(cal, dict) and "Earnings Date" in cal:
                    ed = cal["Earnings Date"]
                    if hasattr(ed, "__iter__") and not isinstance(ed, str):
                        ed = list(ed)[0]
                    earnings_date = ed
                elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                    earnings_date = cal["Earnings Date"].iloc[0]
        except Exception:
            pass

        return {
            "last": float(closes.iloc[-1]),
            "rv_21d_pct": rv_21 * 100,
            "rv_60d_pct": rv_60 * 100,
            "earnings_date": earnings_date,
        }
    except Exception as e:
        print(f"  ! {symbol}: {e}", file=sys.stderr)
        return {}


def get_atm_iv(client, symbol: str, spot: float, expiration: str) -> dict:
    """Pull ATM straddle mark + IV from chain for the given expiration."""
    try:
        ch = client.get_option_chain(OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            expiration_date=expiration,
        ))
    except Exception as e:
        return {"error": str(e)}

    calls = ch.calls or []
    puts = ch.puts or []
    # Find ATM call and put
    atm_call = min(calls, key=lambda c: abs(parse_osi_strike(c.instrument.symbol) - spot), default=None) if calls else None
    atm_put = min(puts, key=lambda p: abs(parse_osi_strike(p.instrument.symbol) - spot), default=None) if puts else None

    result = {"expiration": expiration}
    if atm_call:
        bid = float(atm_call.bid) if atm_call.bid else 0
        ask = float(atm_call.ask) if atm_call.ask else 0
        mark = (bid + ask) / 2 if bid and ask else float(atm_call.last or 0)
        result["atm_call_strike"] = parse_osi_strike(atm_call.instrument.symbol)
        result["atm_call_mark"] = mark
    if atm_put:
        bid = float(atm_put.bid) if atm_put.bid else 0
        ask = float(atm_put.ask) if atm_put.ask else 0
        mark = (bid + ask) / 2 if bid and ask else float(atm_put.last or 0)
        result["atm_put_strike"] = parse_osi_strike(atm_put.instrument.symbol)
        result["atm_put_mark"] = mark
    if atm_call and atm_put:
        result["straddle"] = result["atm_call_mark"] + result["atm_put_mark"]
        result["straddle_pct"] = (result["straddle"] / spot) * 100
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", nargs="+", required=True)
    ap.add_argument("--days-ahead", type=int, default=45,
                    help="Ignore earnings more than N days out")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    client = get_client()
    results = []

    print(f"Scanning {len(args.watchlist)} tickers for upcoming earnings...\n", file=sys.stderr)

    for symbol in args.watchlist:
        symbol = symbol.upper()
        m = get_earnings_and_rv(symbol)
        if not m or not m.get("earnings_date"):
            print(f"  {symbol}: no earnings data", file=sys.stderr)
            continue

        ed = m["earnings_date"]
        if hasattr(ed, "date"):
            ed_date = ed.date()
        else:
            ed_date = ed
        days_to_earnings = (ed_date - date.today()).days
        if days_to_earnings < 0 or days_to_earnings > args.days_ahead:
            print(f"  {symbol}: earnings {ed_date} is {days_to_earnings}d away — skip", file=sys.stderr)
            continue

        print(f"  {symbol}: earnings {ed_date} ({days_to_earnings}d), RV21={m['rv_21d_pct']:.1f}%, spot=${m['last']:.2f}", file=sys.stderr)

        # Find an expiration AFTER earnings (so the IV reflects post-earnings)
        try:
            exps = client.get_option_expirations(OptionExpirationsRequest(
                instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY)
            ))
            exps = sorted([str(e) for e in (exps.expirations or [])])
            post_exp = None
            for e in exps:
                try:
                    e_date = datetime.strptime(e, "%Y-%m-%d").date()
                    if e_date > ed_date:
                        post_exp = e
                        break
                except ValueError:
                    continue
        except Exception as ex:
            print(f"  {symbol}: expirations failed: {ex}", file=sys.stderr)
            continue

        if not post_exp:
            print(f"  {symbol}: no expiration after earnings", file=sys.stderr)
            continue

        # Pull ATM straddle on the post-earnings expiration (this reflects post-event IV)
        atm = get_atm_iv(client, symbol, m["last"], post_exp)
        if "straddle" not in atm:
            print(f"  {symbol}: could not get straddle for {post_exp}", file=sys.stderr)
            continue

        # Expected move = straddle / spot (this is 1-sigma move for the period)
        expected_move_pct = atm["straddle_pct"]
        # IV-vs-RV: implied move at 1-sigma = straddle/spot.
        # RV at same horizon: rv_annual * sqrt(dte/365)
        dte = (datetime.strptime(post_exp, "%Y-%m-%d").date() - date.today()).days
        rv_horizon_pct = m["rv_21d_pct"] * np.sqrt(dte / 21) / np.sqrt(12)  # rescale 21d -> annualized then to horizon
        # Simpler: use rv_21d_pct as the 21d expected move, scale to dte
        rv_21d_move = m["rv_21d_pct"]  # this is annualized %, 1-sigma = rv_annual * sqrt(21/252)
        # 1-sigma daily move = rv_annual/sqrt(252); 1-sigma 21d = rv_annual * sqrt(21/252)
        rv_annual = m["rv_21d_pct"]
        rv_1sigma_daily = rv_annual / np.sqrt(252) * 100  # convert % to decimal
        # Actually rv_21d_pct was already annualized. Expected 1-sigma move over D days = annual% * sqrt(D/252)
        rv_horizon_move = rv_annual * np.sqrt(dte / 252)
        iv_rv_spread = expected_move_pct - rv_horizon_move

        results.append({
            "symbol": symbol,
            "earnings_date": str(ed_date),
            "days_to_earnings": days_to_earnings,
            "spot": m["last"],
            "rv_21d_pct": m["rv_21d_pct"],
            "post_earnings_expiration": post_exp,
            "dte_post": dte,
            "atm_straddle": atm["straddle"],
            "expected_move_pct": expected_move_pct,
            "expected_rv_move_pct": rv_horizon_move,
            "iv_rv_spread_pct": iv_rv_spread,
            "atm_call_strike": atm.get("atm_call_strike"),
            "atm_put_strike": atm.get("atm_put_strike"),
        })

    # Sort by IV-vs-RV spread (richest vol first)
    results.sort(key=lambda r: r["iv_rv_spread_pct"], reverse=True)

    if args.json:
        print(json.dumps({"as_of": datetime.now().isoformat(), "tickers": results}, indent=2, default=str))
        return

    print(f"\n\n{'#'*78}")
    print(f"# EARNINGS IV-CRUSH SCANNER — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"# {len(results)} tickers with earnings in next {args.days_ahead} days")
    print(f"{'#'*78}\n")

    if not results:
        print("  No upcoming earnings match the filter.")
        return

    print(f"  {'Ticker':<6} {'Earnings':<12} {'DTE':>4} {'Spot':>8} {'Straddle':>10} "
          f"{'EMove%':>7} {'RV%':>6} {'Spread':>7}  Action")
    print(f"  {'-'*6} {'-'*12} {'-'*4} {'-'*8} {'-'*10} {'-'*7} {'-'*6} {'-'*7}  ------")
    for r in results:
        # Classify
        if r["iv_rv_spread_pct"] > 5:
            action = "SELL PREMIUM"
        elif r["iv_rv_spread_pct"] < -3:
            action = "BUY STRADDLE"
        else:
            action = "neutral"
        print(f"  {r['symbol']:<6} {r['earnings_date']:<12} {r['days_to_earnings']:>4} "
              f"${r['spot']:>7.2f} ${r['atm_straddle']:>9.2f} {r['expected_move_pct']:>6.1f}% "
              f"{r['rv_21d_pct']:>5.1f}% {r['iv_rv_spread_pct']:>+6.1f}%  {action}")

    print(f"\nLegend: EMove% = ATM straddle / spot (implied).  Spread = EMove - RV.")
    print(f"        SELL PREMIUM = IV > RV by >5% (sell straddle / strangle, expect crush).")
    print(f"        BUY STRADDLE = IV < RV by >3% (cheap vol, expect drift).")


if __name__ == "__main__":
    main()
