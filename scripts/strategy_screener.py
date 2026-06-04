#!/usr/bin/env python3.12
"""
strategy_screener.py — Combined screener + IV + backtest classification.

For each ticker, combines:
  1. Current IV rank (cheap/expensive?)
  2. Days to next earnings (binary event risk)
  3. Backtest tier (Tier 1/2/3 from earnings_backtest_v2 OOS data)
  4. Macro regime (from macro_overlay)

Outputs a single recommendation per ticker: DEPLOY / SMALL_SIZE / SKIP

This is what the daily brief should call. It answers:
  "Given everything I know right now, where should I put the next dollar?"

Usage:
  ./strategy_screener.py --watchlist SPY QQQ NVDA AAPL MSFT TSLA AMZN META GOOGL
  ./strategy_screener.py --watchlist XOM HD PFE GS --backtest-tier-only
  ./strategy_screener.py --tier1-only  # show only Tier 1 names from backtest v2
"""
import argparse
import json
import sys
import subprocess
from datetime import date, timedelta
from pathlib import Path
import numpy as np
import yfinance as yf

SCRIPTS_DIR = Path("/home/chavez_f/.openclaw/workspace/quant-tools/scripts")
PY = "/usr/bin/python3.12"

# Tier 1 / Tier 2 / Tier 3 from earnings_backtest_v2 (24-name OOS study, 2026-06-04)
# Update by re-running: ./quant.py backtest2 --tickers <list> --oos --portfolio
TIER_DATA = {
    # Tier 1: positive OOS Sharpe > 1.5, Win% > 60%, Worst > -2%
    "XOM":  {"tier": 1, "oos_sharpe": 8.58, "oos_win": 100.0, "oos_pf": float("inf")},
    "HD":   {"tier": 1, "oos_sharpe": 6.81, "oos_win": 77.8, "oos_pf": 11.95},
    "PFE":  {"tier": 1, "oos_sharpe": 4.33, "oos_win": 77.8, "oos_pf": 3.24},
    "GS":   {"tier": 1, "oos_sharpe": 3.61, "oos_win": 66.7, "oos_pf": 4.30},
    # Tier 2: positive but mixed
    "KO":   {"tier": 2, "oos_sharpe": 2.44, "oos_win": 44.4, "oos_pf": 1.97},
    "JNJ":  {"tier": 2, "oos_sharpe": 2.11, "oos_win": 55.6, "oos_pf": 2.86},
    "NVDA": {"tier": 2, "oos_sharpe": 1.77, "oos_win": 77.8, "oos_pf": 2.96},
    "MCD":  {"tier": 2, "oos_sharpe": 1.75, "oos_win": 55.6, "oos_pf": 1.62},
    "SBUX": {"tier": 2, "oos_sharpe": 1.61, "oos_win": 55.6, "oos_pf": 1.52},
    "TSLA": {"tier": 2, "oos_sharpe": 1.34, "oos_win": 44.4, "oos_pf": 2.53},
    "AMZN": {"tier": 2, "oos_sharpe": 0.74, "oos_win": 22.2, "oos_pf": 1.53},
    "AAPL": {"tier": 2, "oos_sharpe": 0.62, "oos_win": 33.3, "oos_pf": 2.00},
    "CVX":  {"tier": 2, "oos_sharpe": 0.37, "oos_win": 44.4, "oos_pf": 1.70},
    "BAC":  {"tier": 2, "oos_sharpe": 0.03, "oos_win": 33.3, "oos_pf": 1.33},
    # Tier 3: avoid
    "META": {"tier": 3, "oos_sharpe": -3.03, "oos_win": 22.2, "oos_pf": 0.54},
    "GOOGL": {"tier": 3, "oos_sharpe": -8.74, "oos_win": 22.2, "oos_pf": 0.09},
    "ORCL": {"tier": 3, "oos_sharpe": -7.45, "oos_win": 11.1, "oos_pf": 0.18},
    "ADBE": {"tier": 3, "oos_sharpe": -3.27, "oos_win": 22.2, "oos_pf": 0.52},
    "WMT":  {"tier": 3, "oos_sharpe": -2.24, "oos_win": 22.2, "oos_pf": 0.53},
    "NKE":  {"tier": 3, "oos_sharpe": -2.23, "oos_win": 11.1, "oos_pf": 0.26},
    "NFLX": {"tier": 3, "oos_sharpe": -2.03, "oos_win": 44.4, "oos_pf": 0.40},
    "CRM":  {"tier": 3, "oos_sharpe": -1.73, "oos_win": 33.3, "oos_pf": 0.83},
    "JPM":  {"tier": 3, "oos_sharpe": -1.44, "oos_win": 33.3, "oos_pf": 0.77},
    "MSFT": {"tier": 3, "oos_sharpe": -0.09, "oos_win": 44.4, "oos_pf": 0.79},
}


def get_iv_rank(ticker: str) -> dict:
    """Get IV rank directly from yfinance (faster, more reliable than parsing iv_rank.py output)."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y", auto_adjust=True)
        if hist.empty or len(hist) < 30:
            return {"iv_rank": None, "raw": f"only {len(hist)} days of history"}
        closes = hist["Close"].dropna()
        # 30d rolling realized vol (annualized) as IV proxy
        log_rets = np.log(closes / closes.shift(1)).dropna()
        if len(log_rets) < 60:
            return {"iv_rank": None, "raw": "insufficient log returns"}
        # 30d rolling vol series
        rolling_vol = log_rets.rolling(30).std() * np.sqrt(252) * 100
        rolling_vol = rolling_vol.dropna()
        if len(rolling_vol) < 20:
            return {"iv_rank": None, "raw": "insufficient rolling vol history"}
        current_iv = float(rolling_vol.iloc[-1])
        low_52w = float(rolling_vol.min())
        high_52w = float(rolling_vol.max())
        if high_52w == low_52w:
            return {"iv_rank": 50.0, "raw": f"IV flat at {current_iv:.1f}%"}
        # Tastytrade IV rank: (current - 52w low) / (52w high - 52w low) * 100
        iv_rank = (current_iv - low_52w) / (high_52w - low_52w) * 100
        return {"iv_rank": float(iv_rank), "raw": f"IVR={iv_rank:.0f} (IV={current_iv:.1f}%, range {low_52w:.1f}-{high_52w:.1f})"}
    except Exception as e:
        return {"iv_rank": None, "raw": str(e)}


def get_days_to_earnings(ticker: str) -> int | None:
    """Days until next earnings (from yfinance). None if unknown."""
    try:
        t = yf.Ticker(ticker)
        edf = t.earnings_dates
        if edf is None or edf.empty:
            return None
        # Find the first future date
        today = date.today()
        for d in edf.index:
            d2 = d.date() if hasattr(d, "date") else d
            if d2 > today:
                return (d2 - today).days
        return None
    except Exception:
        return None


def classify(ticker: str, iv_rank: float | None, days_to_earn: int | None,
             macro_regime_score: int | None) -> dict:
    """
    Combine signals into a single verdict.

    Rules:
      - Tier 1 + IVR > 50 + no earnings in 5d + regime >= FAVORABLE (50) = DEPLOY
      - Tier 1 + (IVR 25-50 OR earnings imminent) = SMALL_SIZE (closer to expiry)
      - Tier 1 + IVR < 25 = WAIT (premium too cheap)
      - Tier 2 + good conditions = SMALL_SIZE
      - Tier 2 + bad conditions = WAIT
      - Tier 3 = SKIP regardless
    """
    tier_info = TIER_DATA.get(ticker, {"tier": 0, "oos_sharpe": 0, "oos_win": 0, "oos_pf": 0})

    if tier_info["tier"] == 0:
        return {
            "ticker": ticker,
            "tier": "UNKNOWN",
            "oos_sharpe": None,
            "iv_rank": iv_rank,
            "days_to_earnings": days_to_earn,
            "verdict": "SKIP (no backtest data)",
            "rationale": "Not in backtest universe. Run backtest2 first.",
        }

    if tier_info["tier"] == 3:
        return {
            "ticker": ticker,
            "tier": "3 (AVOID)",
            "oos_sharpe": tier_info["oos_sharpe"],
            "iv_rank": iv_rank,
            "days_to_earnings": days_to_earn,
            "verdict": "SKIP",
            "rationale": f"Tier 3: OOS Sharpe={tier_info['oos_sharpe']:.2f}, Win%={tier_info['oos_win']:.0f}%. No edge out-of-sample.",
        }

    # Tier 1 or 2 — check the rest
    rationale_parts = [f"Tier {tier_info['tier']}, OOS Sharpe={tier_info['oos_sharpe']:.2f}, Win%={tier_info['oos_win']:.0f}%"]

    # IV check
    ivr_str = f"{iv_rank:.0f}" if iv_rank is not None else "?"
    ivr_ok = iv_rank is not None and iv_rank > 50
    ivr_cheap = iv_rank is not None and iv_rank < 25
    if ivr_ok:
        rationale_parts.append(f"IVR={ivr_str} (rich, sell premium)")
    elif ivr_cheap:
        rationale_parts.append(f"IVR={ivr_str} (cheap, premium too small)")
    else:
        rationale_parts.append(f"IVR={ivr_str} (mid-range or unknown)")

    # Earnings window check
    earn_close = days_to_earn is not None and days_to_earn <= 5
    earn_window = days_to_earn is not None and 5 < days_to_earn <= 14
    if earn_close:
        rationale_parts.append(f"Earnings in {days_to_earn}d (too close for fresh strangle)")
    elif earn_window:
        rationale_parts.append(f"Earnings in {days_to_earn}d (ideal pre-earnings window)")
    elif days_to_earn is None:
        rationale_parts.append("No upcoming earnings data")

    # Macro regime
    macro_ok = macro_regime_score is not None and macro_regime_score >= 50
    macro_cautious = macro_regime_score is not None and 35 <= macro_regime_score < 50
    if macro_ok:
        rationale_parts.append(f"Macro regime {macro_regime_score}/100 (FAVORABLE+)")
    elif macro_cautious:
        rationale_parts.append(f"Macro regime {macro_regime_score}/100 (CAUTIOUS, half size)")
    else:
        rationale_parts.append(f"Macro regime {macro_regime_score}/100 (DEFENSIVE)")

    # Verdict logic
    if tier_info["tier"] == 1 and ivr_ok and not earn_close and macro_ok:
        verdict = "DEPLOY"
    elif tier_info["tier"] == 1 and (ivr_cheap or earn_close or not macro_ok):
        verdict = "WAIT"
    elif tier_info["tier"] == 1:
        verdict = "SMALL_SIZE"  # Tier 1 with mixed conditions
    elif tier_info["tier"] == 2 and ivr_ok and earn_window and macro_ok:
        verdict = "SMALL_SIZE"
    else:
        verdict = "WAIT"

    return {
        "ticker": ticker,
        "tier": f"{tier_info['tier']} ({'DEPLOY' if tier_info['tier']==1 else 'SMALL' if tier_info['tier']==2 else 'AVOID'})",
        "oos_sharpe": tier_info["oos_sharpe"],
        "iv_rank": iv_rank,
        "days_to_earnings": days_to_earn,
        "verdict": verdict,
        "rationale": " | ".join(rationale_parts),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", nargs="+", required=True)
    ap.add_argument("--macro-score", type=int, default=50,
                    help="Macro regime score 0-100 (default 50=FAVORABLE). Get from macro_overlay.py.")
    ap.add_argument("--tier1-only", action="store_true")
    ap.add_argument("--backtest-tier-only", action="store_true",
                    help="Skip IV/earnings fetch, just show tier classification")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = []
    for sym in args.watchlist:
        sym = sym.upper()
        tier_info = TIER_DATA.get(sym)
        if args.tier1_only and (not tier_info or tier_info["tier"] != 1):
            continue
        if args.backtest_tier_only:
            results.append({
                "ticker": sym,
                "tier": tier_info["tier"] if tier_info else 0,
                "oos_sharpe": tier_info["oos_sharpe"] if tier_info else None,
                "verdict": "DEPLOY" if tier_info and tier_info["tier"] == 1 else
                           "SMALL" if tier_info and tier_info["tier"] == 2 else "SKIP",
            })
            continue

        print(f"  {sym}: fetching IV + earnings...", file=sys.stderr)
        iv_data = get_iv_rank(sym)
        days = get_days_to_earnings(sym)
        r = classify(sym, iv_data["iv_rank"], days, args.macro_score)
        results.append(r)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
        return

    print(f"\n{'#'*100}")
    print(f"# STRATEGY SCREENER — combined backtest tier + IV + earnings window + macro")
    print(f"# Macro regime score: {args.macro_score}/100")
    print(f"# As of {date.today().isoformat()}")
    print(f"{'#'*100}\n")

    print(f"  {'Ticker':<7} {'Tier':<18} {'IVR':>6} {'DaysToEarn':>11} {'Verdict':<12} Rationale")
    print(f"  {'-'*6} {'-'*18} {'-'*6} {'-'*11} {'-'*12} {'-'*40}")
    for r in results:
        ivr = f"{r['iv_rank']:.0f}" if r.get("iv_rank") is not None else "?"
        dte = str(r.get("days_to_earnings", "?")) if r.get("days_to_earnings") is not None else "?"
        print(f"  {r['ticker']:<7} {r.get('tier','?'):<18} {ivr:>6} {dte:>11}  "
              f"{r['verdict']:<12} {r.get('rationale', '')}")

    # Summary
    verdicts = [r["verdict"] for r in results]
    print(f"\n  Summary: DEPLOY={verdicts.count('DEPLOY')}, "
          f"SMALL_SIZE={verdicts.count('SMALL_SIZE')}, "
          f"WAIT={verdicts.count('WAIT')}, "
          f"SKIP={verdicts.count('SKIP')}")


if __name__ == "__main__":
    main()
