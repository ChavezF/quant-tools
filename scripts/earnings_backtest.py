#!/usr/bin/env python3.12
"""
earnings_backtest.py — Backtest short strangle around historical earnings.

For each ticker:
  1. Find the last N earnings dates (from yfinance)
  2. For each earnings event, compute the 1-day post-earnings stock move
  3. Simulate selling a strangle 5 days before earnings:
     - 16-delta strangle (1-stdev OTM call + put), held to 1 day post-earnings
     - Track P&L as: credit_received - max(0, spot_at_close - call_strike) - max(0, put_strike - spot_at_close)
  4. Aggregate stats: win rate, avg P&L, max loss, P&L per event

This calibrates the size and risk of any future short-vol earnings play.

Approximations:
  - Uses 16-delta strikes approximated by ±1-sigma move (straddle-implied move)
  - Assumes we got the mid of the bid/ask on entry and exit
  - No commissions/slippage modeled

Usage:
  ./earnings_backtest.py --tickers NVDA AAPL MSFT TSLA AMZN META --num-events 8
  ./earnings_backtest.py --tickers NVDA --num-events 12 --json
"""
import argparse
import json
import sys
from datetime import datetime, date, timedelta
import numpy as np
import yfinance as yf


def find_historical_earnings(symbol: str, num_events: int) -> list[date]:
    """Find the last N earnings dates for a ticker from yfinance."""
    try:
        t = yf.Ticker(symbol)
        edf = t.earnings_dates
        if edf is None or edf.empty:
            return []
        # earnings_dates is indexed by date
        dates = sorted([d.date() if hasattr(d, "date") else d for d in edf.index], reverse=True)
        # Only past dates
        past = [d for d in dates if d <= date.today()]
        return past[:num_events]
    except Exception as e:
        print(f"  ! {symbol} earnings history: {e}", file=sys.stderr)
        return []


def get_realized_move(t: yf.Ticker, earnings_date: date, window_days: int = 5) -> dict:
    """For a historical earnings event, get:
       - pre_iv: 5d straddle-implied move (we'll use 20d historical vol proxy)
       - spot_at_entry: close 5d before earnings
       - spot_at_exit: close 1d after earnings
       - move_pct: the realized 1d-post move
       - high_to_low: max intraday range around earnings
    """
    try:
        # Get a window from 10d before to 5d after
        start = earnings_date - timedelta(days=15)
        end = earnings_date + timedelta(days=5)
        hist = t.history(start=start, end=end, auto_adjust=True)
        if hist.empty or len(hist) < 5:
            return {}
        # Pre-earnings close: 5 trading days before
        idx = hist.index
        closes = hist["Close"]
        highs = hist["High"]
        lows = hist["Low"]

        # Find the row closest to (earnings_date - 5 days)
        pre_target = earnings_date - timedelta(days=5)
        pre_idx = idx[idx.date <= pre_target]
        if len(pre_idx) == 0:
            return {}
        pre_close = float(closes.loc[pre_idx[-1]])

        # Find post-earnings: 1 trading day after
        post_target = earnings_date + timedelta(days=1)
        post_idx = idx[idx.date >= post_target]
        if len(post_idx) == 0:
            return {}
        post_close = float(closes.loc[post_idx[0]])

        # 1-day post-earnings move
        move = (post_close - pre_close) / pre_close
        # Absolute
        abs_move = abs(move)

        # Approximate pre-earnings IV via 20d realized vol (annualized)
        # Take 30d window ending at pre-earnings
        # For simplicity, just use the 1d post move as the "what would have happened"

        return {
            "pre_close": pre_close,
            "post_close": post_close,
            "move_pct": move * 100,
            "abs_move_pct": abs_move * 100,
        }
    except Exception as e:
        return {}


def simulate_short_strangle(move_pct: float, abs_move_pct: float, premium_pct: float,
                            strike_distance_pct: float = None,
                            win_threshold: float = 0.0) -> dict:
    """
    Given realized move, simulate a 16-delta short strangle.

    Args:
      move_pct: signed 1d-post-earnings move (positive = up)
      abs_move_pct: absolute value
      premium_pct: total credit collected as % of stock price
      strike_distance_pct: distance from spot to short strikes (e.g. 5% for 16-delta strangle)
                          If None, defaults to premium_pct (a 1:1 approximation)
      win_threshold: P&L above this is a "win"

    Returns:
      dict with pnl_pct, is_win
    """
    if strike_distance_pct is None:
        strike_distance_pct = premium_pct  # rough: 16-delta strike at ~ premium
    # Win zone = within [strike_distance - premium, strike_distance + premium]
    upper_breakeven = strike_distance_pct + premium_pct
    lower_breakeven = -strike_distance_pct - premium_pct
    if move_pct < upper_breakeven and move_pct > lower_breakeven:
        # Within breakeven range — full profit
        pnl = premium_pct
    else:
        # Beyond breakeven — loss = excess
        if move_pct > 0:
            pnl = premium_pct - (move_pct - upper_breakeven)
        else:
            pnl = premium_pct - (abs(move_pct) - strike_distance_pct - premium_pct)
    return {
        "pnl_pct": pnl,
        "is_win": pnl > win_threshold,
    }


def estimate_premium_pct(symbol: str, abs_move_pct_avg: float, current_iv_pct: float = None) -> float:
    """
    Rough estimate of 7-DTE 16-delta strangle premium as % of stock price.

    Empirical relationship for short-dated 16-delta strangles:
      premium ≈ 0.8x the 1-sigma 7-DTE move
      7-DTE 1-sigma move = annual_IV * sqrt(7/365)

    Without current_iv, fall back to abs_move_pct_avg * 0.8 (assumes realized
    vol ~ IV, which is roughly true over a 1y lookback).
    """
    if current_iv_pct and current_iv_pct > 5:
        one_sigma_pct = current_iv_pct * (7 / 365) ** 0.5
        return one_sigma_pct * 0.8
    return abs_move_pct_avg * 0.8  # unbiased fallback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="+", required=True)
    ap.add_argument("--num-events", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = {"as_of": datetime.now().isoformat(), "tickers": {}}

    for sym in args.tickers:
        sym = sym.upper()
        print(f"  {sym}: pulling {args.num_events} historical earnings...", file=sys.stderr)

        try:
            t = yf.Ticker(sym)
        except Exception as e:
            print(f"  ! {sym}: yfinance failed: {e}", file=sys.stderr)
            continue

        earnings_dates = find_historical_earnings(sym, args.num_events)
        if not earnings_dates:
            print(f"  ! {sym}: no earnings history", file=sys.stderr)
            continue

        events = []
        for ed in earnings_dates:
            move = get_realized_move(t, ed)
            if not move:
                continue
            events.append({
                "earnings_date": str(ed),
                "pre_close": round(move["pre_close"], 2),
                "post_close": round(move["post_close"], 2),
                "move_pct": round(move["move_pct"], 2),
                "abs_move_pct": round(move["abs_move_pct"], 2),
            })

        if not events:
            print(f"  ! {sym}: no valid events", file=sys.stderr)
            continue

        # Stats
        moves = [e["abs_move_pct"] for e in events]
        avg_abs_move = float(np.mean(moves))
        median_abs_move = float(np.median(moves))
        max_abs_move = float(np.max(moves))
        std_abs_move = float(np.std(moves))

        # Estimate strangle premium (use current IV if available, else proxy)
        est_premium_pct = estimate_premium_pct(sym, avg_abs_move)

        # 16-delta strike distance: ~ 1.0-1.2 * 1-sigma move. Use 1.1x.
        est_strike_distance_pct = est_premium_pct * 1.1

        # Simulate
        sim_results = []
        for e in events:
            sim = simulate_short_strangle(
                e["move_pct"], e["abs_move_pct"], est_premium_pct,
                strike_distance_pct=est_strike_distance_pct,
            )
            sim_results.append(sim)

        wins = sum(1 for s in sim_results if s["is_win"])
        win_rate = wins / len(sim_results) if sim_results else 0
        pnls = [s["pnl_pct"] for s in sim_results]
        avg_pnl = float(np.mean(pnls))
        worst_pnl = float(np.min(pnls))
        best_pnl = float(np.max(pnls))
        cumulative_pnl = float(np.sum(pnls))
        # Max drawdown (cumulative P&L curve)
        cumulative = np.cumsum(pnls)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

        results["tickers"][sym] = {
            "num_events": len(events),
            "events": events,
            "stats": {
                "avg_abs_move_pct": round(avg_abs_move, 2),
                "median_abs_move_pct": round(median_abs_move, 2),
                "max_abs_move_pct": round(max_abs_move, 2),
                "std_abs_move_pct": round(std_abs_move, 2),
                "est_strangle_premium_pct": round(est_premium_pct, 2),
            },
            "short_strangle_simulation": {
                "win_rate": round(win_rate * 100, 1),
                "avg_pnl_pct": round(avg_pnl, 2),
                "best_pnl_pct": round(best_pnl, 2),
                "worst_pnl_pct": round(worst_pnl, 2),
                "cumulative_pnl_pct": round(cumulative_pnl, 2),
                "max_drawdown_pct": round(max_drawdown, 2),
                "verdict": _verdict(win_rate, avg_pnl, worst_pnl, max_abs_move, est_premium_pct),
            }
        }

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\n\n{'#'*78}")
    print(f"# EARNINGS BACKTEST — short 16Δ strangle entered 5d before earnings")
    print(f"# Hold to 1d post-earnings. Premium estimated as 1.2x avg abs move.")
    print(f"# {'as of ' + datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    print(f"{'#'*78}\n")

    print(f"  {'Ticker':<6} {'Events':>7} {'AvgMove':>8} {'MaxMove':>8} {'Premium':>8} "
          f"{'WinRate':>8} {'AvgPnL':>8} {'Worst':>8} {'MaxDD':>8} {'Cumul':>8}  Verdict")
    print(f"  {'-'*6} {'-'*7} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8} {'-'*8}  -------")
    for sym, r in results["tickers"].items():
        s = r["stats"]
        b = r["short_strangle_simulation"]
        print(f"  {sym:<6} {r['num_events']:>7} {s['avg_abs_move_pct']:>7.1f}% "
              f"{s['max_abs_move_pct']:>7.1f}% {s['est_strangle_premium_pct']:>7.1f}% "
              f"{b['win_rate']:>7.1f}% {b['avg_pnl_pct']:>+7.2f}% {b['worst_pnl_pct']:>+7.2f}% "
              f"{b['max_drawdown_pct']:>+7.2f}% {b['cumulative_pnl_pct']:>+7.2f}%  {b['verdict']}")

    print(f"\n  Reading: WinRate > 60% with positive AvgPnL = short strangle is profitable on this name.")
    print(f"          WinRate > 75% with low WorstPnL = high-conviction earnings play.")
    print(f"          WinRate < 50% or WorstPnL < -10% = avoid naked short vol, use defined risk (iron condor).")


def _verdict(win_rate: float, avg_pnl: float, worst_pnl: float,
             max_abs_move: float, est_premium: float) -> str:
    if win_rate >= 0.75 and avg_pnl > 0 and worst_pnl > -5:
        return "STRONG: short strangle works"
    if win_rate >= 0.60 and avg_pnl > 0:
        return "OK: profitable, size small"
    if win_rate >= 0.50 and avg_pnl > 0:
        return "BORDERLINE: use defined risk"
    return "AVOID: short strangle bleeds"


if __name__ == "__main__":
    main()
