#!/usr/bin/env python3.12
"""
macro_overlay.py — Macro & vol regime scoring for the next 5 trading days.

Inputs:
  - VIX level + 1w/1m change + 1y percent rank
  - Yield curve (10Y-2Y, 10Y-3M) for recession risk
  - DXY 1m trend (impacts multinational earnings)
  - Watchlist earnings in next 7 days with expected move
  - Forward vol forecast = blended spot-VIX + recent-RV

Output:
  - A "regime score" 0-100 where higher = more opportunity for short premium
  - Earnings calendar with expected move vs current IV
  - Specific recommendations (e.g. "Cautious selling this week, NVDA earnings Wed")

Usage:
  ./macro_overlay.py --watchlist SPY QQQ NVDA AAPL MSFT TSLA AMD AMZN META GOOGL
  ./macro_overlay.py --watchlist SPY QQQ --json
"""
import argparse
import json
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
import numpy as np
import yfinance as yf


def get_vix_regime() -> dict:
    """Pull VIX spot, 1w/1m changes, 1y percent rank."""
    try:
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="1y", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"]
        spot = float(closes.iloc[-1])
        pct_1w = (spot / float(closes.iloc[-5]) - 1) * 100 if len(closes) >= 5 else 0
        pct_1m = (spot / float(closes.iloc[-21]) - 1) * 100 if len(closes) >= 21 else 0
        pct_rank = float((closes < spot).sum() / len(closes) * 100)
        return {
            "spot": round(spot, 2),
            "pct_1w": round(pct_1w, 2),
            "pct_1m": round(pct_1m, 2),
            "pct_rank_1y": round(pct_rank, 1),
            "low_52w": round(float(closes.min()), 2),
            "high_52w": round(float(closes.max()), 2),
        }
    except Exception as e:
        print(f"  ! VIX failed: {e}", file=sys.stderr)
        return {}


def get_yield_curve() -> dict:
    """Pull 10Y-2Y and 10Y-3M spreads. Negative = inversion (recession signal)."""
    try:
        out = {}
        for series, name in [("^TNX", "10Y"), ("^FVX", "5Y"), ("^IRX", "13W")]:
            t = yf.Ticker(series)
            hist = t.history(period="5d", auto_adjust=True)
            if hist.empty:
                continue
            out[name] = float(hist["Close"].iloc[-1])
        spreads = {}
        if "10Y" in out and "5Y" in out:
            spreads["10Y-5Y_bps"] = round((out["10Y"] - out["5Y"]) * 100, 0)
        # 2Y is harder to get directly; use 5Y as proxy if needed
        # Real 2Y: ^TNX is 10Y, ^FVX is 5Y, ^IRX is 13W
        # We'll do 10Y vs 13W (3M) as the steepest measure
        if "10Y" in out and "13W" in out:
            spreads["10Y-3M_bps"] = round((out["10Y"] - out["13W"]) * 100, 0)
        return {
            "yields_pct": {k: round(v, 3) for k, v in out.items()},
            "spreads_bps": spreads,
        }
    except Exception as e:
        print(f"  ! yield curve failed: {e}", file=sys.stderr)
        return {}


def get_dxy_trend() -> dict:
    """DXY 1m trend. Strong dollar = headwind for multinationals."""
    try:
        dxy = yf.Ticker("DX-Y.NYB")
        hist = dxy.history(period="3mo", auto_adjust=True)
        if hist.empty:
            return {}
        closes = hist["Close"]
        spot = float(closes.iloc[-1])
        pct_1m = (spot / float(closes.iloc[-21]) - 1) * 100 if len(closes) >= 21 else 0
        return {
            "spot": round(spot, 2),
            "pct_1m": round(pct_1m, 2),
        }
    except Exception as e:
        print(f"  ! DXY failed: {e}", file=sys.stderr)
        return {}


def get_upcoming_earnings(watchlist: list, days: int = 7) -> list[dict]:
    """Earnings in next N days, with each name's 1-sigma expected move
    approximated as recent 21d realized vol * sqrt(1/252) * spot."""
    out = []
    for sym in watchlist:
        sym = sym.upper()
        try:
            t = yf.Ticker(sym)
            cal = t.calendar
            ed = None
            if cal is None or (hasattr(cal, "empty") and cal.empty):
                continue
            if isinstance(cal, dict) and "Earnings Date" in cal:
                ed = cal["Earnings Date"]
                if hasattr(ed, "__iter__") and not isinstance(ed, str):
                    ed = list(ed)[0]
            elif hasattr(cal, "columns") and "Earnings Date" in cal.columns:
                ed = cal["Earnings Date"].iloc[0]
            if not ed:
                continue
            if hasattr(ed, "date"):
                ed = ed.date()
            dte = (ed - date.today()).days
            if not (0 <= dte <= days):
                continue

            # 1-sigma expected move = RV_21d_annual * sqrt(1/252) * spot
            hist = t.history(period="3mo", auto_adjust=True)
            if hist.empty:
                continue
            closes = hist["Close"]
            log_returns = np.log(closes / closes.shift(1)).dropna()
            rv_21 = float(log_returns.tail(21).std() * np.sqrt(252) * 100)
            spot = float(closes.iloc[-1])
            emove_pct = rv_21 / np.sqrt(252)
            emove_dollar = spot * emove_pct / 100
            out.append({
                "ticker": sym,
                "date": str(ed),
                "days": dte,
                "spot": round(spot, 2),
                "rv_21d_pct": round(rv_21, 1),
                "expected_move_pct": round(emove_pct, 2),
                "expected_move_dollar": round(emove_dollar, 2),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["days"])
    return out


def compute_regime_score(vix: dict, yield_curve: dict, dxy: dict,
                         earnings: list[dict]) -> dict:
    """
    Compute a 0-100 regime score for short-premium opportunity.

    Higher = better for short premium (high IV, no imminent binary events, no curve inversion)
    Lower = worse (low IV, earnings cluster, recession risk)

    Components:
      - VIX spot: 20-25% = perfect, <15 = bad for selling, >30 = dangerous
      - VIX trend: rising = wait, falling = sell
      - VIX 1y rank: high = good
      - Yield curve: not inverted = ok
      - DXY: not spiking = ok
      - Earnings density: more events = more risk
    """
    score = 50  # neutral
    reasons = []

    if vix:
        spot = vix.get("spot", 0)
        rank = vix.get("pct_rank_1y", 50)
        # VIX sweet spot
        if 20 <= spot <= 25:
            score += 15
            reasons.append(f"VIX {spot} in sweet spot (20-25)")
        elif spot < 15:
            score -= 15
            reasons.append(f"VIX {spot} too low — premium is cheap")
        elif spot > 30:
            score -= 10
            reasons.append(f"VIX {spot} elevated — reduce size")
        # VIX 1y rank
        if rank > 70:
            score += 10
            reasons.append(f"VIX 1y rank {rank} (high)")
        elif rank < 30:
            score -= 10
            reasons.append(f"VIX 1y rank {rank} (low)")
        # VIX trend
        if vix.get("pct_1w", 0) > 10:
            score -= 10
            reasons.append(f"VIX +{vix['pct_1w']}% WoW (rising fast)")
        elif vix.get("pct_1w", 0) < -10:
            score += 5
            reasons.append(f"VIX {vix['pct_1w']}% WoW (falling)")

    if yield_curve:
        spreads = yield_curve.get("spreads_bps", {})
        # 10Y-3M inversion: bad
        if spreads.get("10Y-3M_bps", 0) < 0:
            score -= 15
            reasons.append(f"10Y-3M inverted: {spreads['10Y-3M_bps']}bps (recession risk)")

    if dxy:
        if dxy.get("pct_1m", 0) > 3:
            score -= 5
            reasons.append(f"DXY +{dxy['pct_1m']}% 1m (dollar headwind)")

    # Earnings density: more than 3 in next 7d = reduce
    if len(earnings) >= 4:
        score -= 10
        reasons.append(f"{len(earnings)} watchlist earnings in next 7d")
    elif len(earnings) == 0:
        score += 5
        reasons.append("No watchlist earnings this week — clean window")

    score = max(0, min(100, score))

    if score >= 65:
        verdict = "AGGRESSIVE: scale up short premium"
    elif score >= 50:
        verdict = "FAVORABLE: normal sizing"
    elif score >= 35:
        verdict = "CAUTIOUS: half size, skip earnings names"
    else:
        verdict = "DEFENSIVE: cash > premium, wait for setup"

    return {
        "score": score,
        "verdict": verdict,
        "reasons": reasons,
    }


def build_report(vix: dict, yield_curve: dict, dxy: dict,
                 earnings: list[dict], regime: dict, watchlist: list) -> str:
    lines = []
    lines.append(f"\n{'#'*78}")
    lines.append(f"# MACRO OVERLAY — {datetime.now().strftime('%Y-%m-%d %H:%M ET')}")
    lines.append(f"# Regime score: {regime['score']}/100  →  {regime['verdict']}")
    lines.append(f"{'#'*78}\n")

    # VIX
    if vix:
        lines.append(f"📊 VIX")
        lines.append(f"  Spot:       {vix['spot']:.2f}  (1w: {vix['pct_1w']:+.1f}%  1m: {vix['pct_1m']:+.1f}%)")
        lines.append(f"  1y range:   {vix['low_52w']:.1f} - {vix['high_52w']:.1f}  (current at {vix['pct_rank_1y']:.0f}%-percentile)")
        lines.append("")

    # Yield curve
    if yield_curve:
        lines.append(f"📈 YIELD CURVE")
        for series, val in yield_curve.get("yields_pct", {}).items():
            lines.append(f"  {series:<6} {val:.3f}%")
        for spread, val in yield_curve.get("spreads_bps", {}).items():
            flag = " ⚠️ INVERTED" if val < 0 else ""
            lines.append(f"  {spread:<12} {val:+.0f}bps{flag}")
        lines.append("")

    # DXY
    if dxy:
        lines.append(f"💵 DXY")
        lines.append(f"  Spot: {dxy['spot']:.2f}  (1m: {dxy['pct_1m']:+.1f}%)")
        lines.append("")

    # Earnings
    lines.append(f"📅 EARNINGS (next 7d)")
    if not earnings:
        lines.append("  (no watchlist earnings in next 7 days)")
    for e in earnings:
        urgency = "🚨" if e["days"] <= 2 else "⚠️ " if e["days"] <= 4 else "  "
        lines.append(f"  {urgency} {e['ticker']:<6} {e['date']} ({e['days']}d)  "
                    f"~{e['expected_move_pct']:>4.1f}% move  (${e['expected_move_dollar']:.2f} on ${e['spot']:.0f} stock)")
    lines.append("")

    # Regime reasoning
    lines.append(f"🎯 REGIME: {regime['score']}/100 — {regime['verdict']}")
    for r in regime["reasons"]:
        lines.append(f"  • {r}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", nargs="+",
                    default=["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD", "AMZN", "META", "GOOGL"])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    vix = get_vix_regime()
    yield_curve = get_yield_curve()
    dxy = get_dxy_trend()
    earnings = get_upcoming_earnings(args.watchlist, args.days)
    regime = compute_regime_score(vix, yield_curve, dxy, earnings)

    if args.json:
        print(json.dumps({
            "as_of": datetime.now().isoformat(),
            "vix": vix,
            "yield_curve": yield_curve,
            "dxy": dxy,
            "earnings": earnings,
            "regime": regime,
        }, indent=2, default=str))
        return

    print(build_report(vix, yield_curve, dxy, earnings, regime, args.watchlist))


if __name__ == "__main__":
    main()
