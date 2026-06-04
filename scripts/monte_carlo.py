#!/usr/bin/env python3.12
"""
monte_carlo.py — Stress-test the 16Δ short strangle against tail events.

Models what happens to a position when IV spikes, gaps happen, or correlations
break. Uses three approaches:

  1. PARAMETRIC: assume stock returns are log-normal with the historical mean/vol.
     Cheap, gives quick distributional view, but misses fat tails.

  2. BOOTSTRAP: resample historical 1d returns with replacement. Preserves the
     empirical fat-tail distribution of the actual stock.

  3. STRESS SCENARIOS: replay specific historical shocks (GFC 2008, COVID 2020,
     2022 bear, Aug 2024 carry unwind, etc.) and ask "if I'd held a 16Δ strangle
     through this, what would I have lost?"

For each method, simulate 10,000 strangle positions, each held for N days, and
report the distribution of outcomes: 5th/50th/95th/99th percentile P&L, ruin
probability, expected max drawdown.

Usage:
  ./monte_carlo.py --ticker NVDA --num-simulations 10000 --hold-days 5
  ./monte_carlo.py --ticker SPY --num-simulations 10000 --method bootstrap --tail-events
  ./monte_carlo.py --ticker NVDA --tail-events  # just the historical replay
"""
import argparse
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf


# Historical shock windows (start_date, end_date, label) for replay
HISTORICAL_SHOCKS = [
    ("2008-09-15", "2008-10-15", "GFC — Lehman collapse (Sep-Oct 2008)"),
    ("2008-10-15", "2008-11-20", "GFC — Crash bottom (Oct-Nov 2008)"),
    ("2011-08-01", "2011-08-15", "US debt downgrade / flash crash"),
    ("2015-08-18", "2015-08-25", "China devaluation / Aug 2015 flash"),
    ("2016-01-15", "2016-02-15", "Oil crash / China slowdown"),
    ("2018-12-01", "2018-12-31", "Dec 2018 Fed-pivot selloff"),
    ("2020-02-20", "2020-03-23", "COVID crash"),
    ("2020-03-23", "2020-06-30", "COVID recovery rally"),
    ("2022-01-04", "2022-10-12", "2022 bear market (peak-to-trough)"),
    ("2023-08-01", "2023-10-31", "2023 Q3 rate spike (long vol pays)"),
    ("2024-08-05", "2024-08-08", "Aug 2024 carry unwind / yen spike"),
    ("2025-04-02", "2025-04-09", "Apr 2025 tariff shock"),
]


def get_historical_1d_returns(symbol: str, lookback_years: int = 10) -> np.ndarray:
    """Pull 1d log returns for the last N years."""
    end = date.today()
    start = end - timedelta(days=365 * lookback_years)
    t = yf.Ticker(symbol)
    hist = t.history(start=start, end=end, auto_adjust=True)
    if hist.empty:
        return np.array([])
    closes = hist["Close"].dropna()
    log_returns = np.log(closes / closes.shift(1)).dropna()
    return log_returns.values


def get_historical_iv_series(symbol: str, lookback_years: int = 5) -> np.ndarray:
    """Pull 30d rolling realized vol as a proxy for IV history."""
    rets = get_historical_1d_returns(symbol, lookback_years)
    if len(rets) < 30:
        return np.array([])
    # 30d rolling vol annualized
    series = pd.Series(rets).rolling(30).std() * np.sqrt(252) * 100
    return series.dropna().values


def parametric_simulation(current_iv_pct: float, hold_days: int,
                          num_sims: int, spot: float) -> dict:
    """
    Log-normal return simulation. Simple but underestimates tails.
    For a 16Δ strangle held over `hold_days`, P&L = premium - max(0, abs(move) - strike_distance)
    (linear approx).
    """
    if current_iv_pct <= 5:
        return {"error": "IV too low, can't price"}
    daily_vol = current_iv_pct / math.sqrt(252) / 100  # decimal
    hold_vol = daily_vol * math.sqrt(hold_days)
    strike_distance_pct = hold_vol * 100  # 16Δ ≈ 1σ of remaining life
    premium_pct = (current_iv_pct / 100) * math.sqrt(hold_days / 252) * 0.6 * 100

    # Simulate returns
    rng = np.random.default_rng(42)
    moves = rng.normal(0, hold_vol, num_sims) * 100  # in %

    pnls = []
    breached = 0
    for m in moves:
        if abs(m) <= strike_distance_pct:
            pnls.append(premium_pct)
        else:
            excess = abs(m) - strike_distance_pct - premium_pct
            pnl = premium_pct - excess
            pnl = max(pnl, -strike_distance_pct)
            pnls.append(pnl)
            if abs(m) > strike_distance_pct:
                breached += 1

    pnls = np.array(pnls)
    return {
        "method": "parametric (log-normal)",
        "n_sims": num_sims,
        "hold_days": hold_days,
        "iv_used_pct": current_iv_pct,
        "premium_pct_per_trade": round(premium_pct, 3),
        "strike_distance_pct": round(strike_distance_pct, 3),
        "p5_pnl_pct": round(float(np.percentile(pnls, 5)), 3),
        "p50_pnl_pct": round(float(np.percentile(pnls, 50)), 3),
        "p95_pnl_pct": round(float(np.percentile(pnls, 95)), 3),
        "p99_pnl_pct": round(float(np.percentile(pnls, 99)), 3),
        "mean_pnl_pct": round(float(pnls.mean()), 3),
        "std_pnl_pct": round(float(pnls.std()), 3),
        "breach_rate": round(breached / num_sims * 100, 1),
        "ruin_prob_pnl_lt_-2pct": round(float((pnls < -2).mean()) * 100, 2),
        "ruin_prob_pnl_lt_-5pct": round(float((pnls < -5).mean()) * 100, 2),
    }


def bootstrap_simulation(historical_returns: np.ndarray, current_iv_pct: float,
                         hold_days: int, num_sims: int) -> dict:
    """
    Resample historical 1d returns with replacement, then compound over hold_days.
    Preserves fat tails. Use a long lookback (10y) for the empirical distribution.
    """
    if current_iv_pct <= 5 or len(historical_returns) < 100:
        return {"error": "insufficient data"}
    daily_vol_emp = float(np.std(historical_returns))
    if daily_vol_emp <= 0:
        return {"error": "zero vol"}
    # Scale to current IV regime (so the simulation reflects TODAY's vol, not historical avg)
    target_daily_vol = current_iv_pct / 100 / math.sqrt(252)
    scale = target_daily_vol / daily_vol_emp
    scaled_returns = historical_returns * scale
    hold_vol = target_daily_vol * math.sqrt(hold_days)
    strike_distance_pct = hold_vol * 100
    premium_pct = (current_iv_pct / 100) * math.sqrt(hold_days / 252) * 0.6 * 100

    rng = np.random.default_rng(42)
    pnls = []
    breached = 0
    for _ in range(num_sims):
        # Resample `hold_days` returns, compound them
        idx = rng.integers(0, len(scaled_returns), size=hold_days)
        sampled = scaled_returns[idx]
        compounded = float(np.prod(1 + sampled) - 1) * 100
        if abs(compounded) <= strike_distance_pct:
            pnls.append(premium_pct)
        else:
            excess = abs(compounded) - strike_distance_pct - premium_pct
            pnl = premium_pct - excess
            pnl = max(pnl, -strike_distance_pct)
            pnls.append(pnl)
            breached += 1

    pnls = np.array(pnls)
    return {
        "method": f"bootstrap (scaled to current IV {current_iv_pct:.1f}%, {len(historical_returns)} historical returns)",
        "n_sims": num_sims,
        "hold_days": hold_days,
        "iv_used_pct": current_iv_pct,
        "premium_pct_per_trade": round(premium_pct, 3),
        "strike_distance_pct": round(strike_distance_pct, 3),
        "p5_pnl_pct": round(float(np.percentile(pnls, 5)), 3),
        "p50_pnl_pct": round(float(np.percentile(pnls, 50)), 3),
        "p95_pnl_pct": round(float(np.percentile(pnls, 95)), 3),
        "p99_pnl_pct": round(float(np.percentile(pnls, 99)), 3),
        "p1_pnl_pct": round(float(np.percentile(pnls, 1)), 3),  # 1st percentile — the tail
        "mean_pnl_pct": round(float(pnls.mean()), 3),
        "std_pnl_pct": round(float(pnls.std()), 3),
        "breach_rate": round(breached / num_sims * 100, 1),
        "ruin_prob_pnl_lt_-2pct": round(float((pnls < -2).mean()) * 100, 2),
        "ruin_prob_pnl_lt_-5pct": round(float((pnls < -5).mean()) * 100, 2),
        "ruin_prob_pnl_lt_-10pct": round(float((pnls < -10).mean()) * 100, 2),
    }


def historical_replay(symbol: str, spot: float, current_iv_pct: float,
                      hold_days: int = 5) -> list[dict]:
    """
    For each historical shock window, simulate a 16Δ short strangle entered at
    the start of the window and held to the end (or 5 trading days, whichever
    comes first). Reports the P&L.
    """
    if current_iv_pct <= 5:
        return [{"error": "IV too low"}]
    daily_vol = current_iv_pct / 100 / math.sqrt(252)
    hold_vol = daily_vol * math.sqrt(hold_days)
    strike_distance_pct = hold_vol * 100
    premium_pct = (current_iv_pct / 100) * math.sqrt(hold_days / 252) * 0.6 * 100

    results = []
    t = yf.Ticker(symbol)
    for start_str, end_str, label in HISTORICAL_SHOCKS:
        try:
            start = date.fromisoformat(start_str)
            end = date.fromisoformat(end_str)
            actual_hold = min(hold_days, (end - start).days)
            hist = t.history(start=start, end=end + timedelta(days=2), auto_adjust=True)
            if hist.empty or len(hist) < 2:
                continue
            closes = hist["Close"]
            # Entry at start close
            entry_price = float(closes.iloc[0])
            # Exit at min(hold_days, end of window) — pick the close at index
            exit_idx = min(actual_hold, len(closes) - 1)
            exit_price = float(closes.iloc[exit_idx])
            move_pct = (exit_price - entry_price) / entry_price * 100
            if abs(move_pct) <= strike_distance_pct:
                pnl = premium_pct
                outcome = "FULL PROFIT"
            else:
                excess = abs(move_pct) - strike_distance_pct - premium_pct
                pnl = max(premium_pct - excess, -strike_distance_pct)
                outcome = "BREACHED" if pnl < premium_pct * 0.5 else "PARTIAL"
            results.append({
                "event": label,
                "window": f"{start_str} → {end_str}",
                "hold_days": exit_idx,
                "move_pct": round(move_pct, 2),
                "premium_pct": round(premium_pct, 3),
                "strike_distance_pct": round(strike_distance_pct, 3),
                "pnl_pct": round(pnl, 3),
                "outcome": outcome,
            })
        except Exception as e:
            results.append({"event": label, "error": str(e)})
    return results


def get_current_iv(symbol: str) -> tuple[float, float]:
    """Return (current_iv_pct, spot). Falls back to 30d RV if no IV available."""
    t = yf.Ticker(symbol)
    try:
        info = t.info or {}
        spot = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
    except Exception:
        spot = 0.0
    # IV isn't reliable from yfinance — use 30d RV as proxy
    hist = t.history(period="6mo", auto_adjust=True)
    if hist.empty:
        return 30.0, spot  # default 30% if nothing
    closes = hist["Close"].dropna()
    if len(closes) < 30:
        return 30.0, spot
    log_rets = np.log(closes / closes.shift(1)).dropna()
    iv_30d = float(log_rets[-30:].std() * np.sqrt(252) * 100)
    if spot == 0:
        spot = float(closes.iloc[-1])
    return iv_30d, spot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--num-simulations", type=int, default=10000)
    ap.add_argument("--hold-days", type=int, default=5)
    ap.add_argument("--method", choices=["parametric", "bootstrap", "both"], default="both")
    ap.add_argument("--tail-events", action="store_true", help="Include historical shock replay")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    sym = args.ticker.upper()
    print(f"  {sym}: pulling data...", file=sys.stderr)
    current_iv, spot = get_current_iv(sym)
    if spot == 0:
        print(f"  ! {sym}: no spot price", file=sys.stderr)
        return
    print(f"  {sym}: spot=${spot:.2f}, 30d RV={current_iv:.1f}%", file=sys.stderr)

    out = {
        "ticker": sym,
        "spot": round(spot, 2),
        "current_iv_30d_pct": round(current_iv, 2),
        "as_of": str(date.today()),
        "simulations": [],
    }

    if args.method in ("parametric", "both"):
        out["simulations"].append(parametric_simulation(current_iv, args.hold_days, args.num_simulations, spot))
    if args.method in ("bootstrap", "both"):
        rets = get_historical_1d_returns(sym, 10)
        if len(rets) > 100:
            out["simulations"].append(bootstrap_simulation(rets, current_iv, args.hold_days, args.num_simulations))
    if args.tail_events:
        out["historical_replay"] = historical_replay(sym, spot, current_iv, args.hold_days)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return

    # Human-readable
    print(f"\n{'#'*100}")
    print(f"# MONTE CARLO — {sym} 16Δ short strangle, {args.num_simulations} sims, {args.hold_days}d hold")
    print(f"# Spot ${spot:.2f}, current 30d RV {current_iv:.1f}% (IV proxy)")
    print(f"{'#'*100}\n")

    for sim in out.get("simulations", []):
        if "error" in sim:
            print(f"  ! {sim['method']}: {sim['error']}")
            continue
        print(f"  Method: {sim['method']}")
        print(f"    Premium collected per trade: {sim['premium_pct_per_trade']:.3f}%")
        print(f"    Strike distance (1σ of hold period): {sim['strike_distance_pct']:.3f}%")
        print(f"    P&L distribution (% of notional):")
        print(f"      1%  worst: {sim.get('p1_pnl_pct', sim['p5_pnl_pct']):+.3f}%")
        print(f"      5%  worst: {sim['p5_pnl_pct']:+.3f}%")
        print(f"     50% median: {sim['p50_pnl_pct']:+.3f}%")
        print(f"     95% best:   {sim['p95_pnl_pct']:+.3f}%")
        print(f"     99% best:   {sim['p99_pnl_pct']:+.3f}%")
        print(f"    Mean: {sim['mean_pnl_pct']:+.3f}%, Std: {sim['std_pnl_pct']:.3f}%")
        print(f"    Breach rate: {sim['breach_rate']:.1f}%")
        print(f"    Ruin probabilities:")
        print(f"      P&L < -2%:  {sim.get('ruin_prob_pnl_lt_-2pct', 0):.2f}%")
        print(f"      P&L < -5%:  {sim.get('ruin_prob_pnl_lt_-5pct', 0):.2f}%")
        if 'ruin_prob_pnl_lt_-10pct' in sim:
            print(f"      P&L < -10%: {sim['ruin_prob_pnl_lt_-10pct']:.2f}%")
        print()

    if "historical_replay" in out and out["historical_replay"]:
        print(f"  HISTORICAL REPLAY (if you'd held a 16Δ strangle through these shocks):")
        for ev in out["historical_replay"]:
            if "error" in ev:
                print(f"    ! {ev['event']}: {ev['error']}")
            else:
                print(f"    {ev['event']:50s} move={ev['move_pct']:+7.2f}%  PnL={ev['pnl_pct']:+7.3f}%  {ev['outcome']}")
        print()

    print(f"  Reading guide:")
    print(f"    The '95% worst' / '5% worst' / '1% worst' lines are the most important.")
    print(f"    If 1% worst is worse than -3%, the strategy has tail risk that 8-event backtests hide.")
    print(f"    Compare PARAMETRIC vs BOOTSTRAP — if bootstrap shows worse tails, the empirical")
    print(f"    distribution has fat tails the log-normal assumption misses.")


if __name__ == "__main__":
    main()
