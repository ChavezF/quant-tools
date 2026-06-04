#!/usr/bin/env python3.12
"""
term_structure.py — ATM IV across expirations for a single ticker.

For each available expiration:
  - Pull ATM straddle
  - Compute IV = straddle / spot / sqrt(dte/365)
  - Print IV-by-DTE curve in ASCII

Flags:
  - Backwardation: near-term IV > far-term (fear, near-term premium rich)
  - Contango: far-term IV > near-term (normal, calm)
  - Flat: similar across the curve

Use cases:
  - If 7-14 DTE is rich but 30-45 DTE is normal → sell the near-term
  - If the curve is in steep backwardation → consider calendars (long far, short near)
  - If the curve is steep contango → sell far-dated premium (e.g. 60-90 DTE)

Usage:
  ./term_structure.py --ticker SPY
  ./term_structure.py --ticker NVDA --max-expirations 12
  ./term_structure.py --ticker AAPL --json
"""
import argparse
import json
import sys
from datetime import datetime, date
from pathlib import Path
import numpy as np
import yfinance as yf

SCRIPTS_DIR = Path("/home/chavez_f/.hermes/skills/openclaw-imports/public-dot-com/scripts")
sys.path.insert(0, str(SCRIPTS_DIR))
from config import get_api_secret, get_account_id

from public_api_sdk import (
    PublicApiClient, PublicApiClientConfiguration,
    OrderInstrument, InstrumentType, OptionChainRequest, OptionExpirationsRequest,
)
from public_api_sdk.auth_config import ApiKeyAuthConfig


def get_client():
    secret = get_api_secret()
    if not secret:
        print("Error: PUBLIC_COM_SECRET missing.", file=sys.stderr)
        sys.exit(1)
    return PublicApiClient(
        ApiKeyAuthConfig(api_secret_key=secret),
        config=PublicApiClientConfiguration(default_account_number=get_account_id() or ""),
    )


def parse_osi_strike(osi: str) -> float:
    try:
        return int(osi[-8:]) / 1000.0
    except (ValueError, IndexError):
        return 0.0


def fetch_expirations(client, symbol: str) -> list[str]:
    try:
        res = client.get_option_expirations(OptionExpirationsRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY)
        ))
        return [str(e) for e in (res.expirations or [])]
    except Exception:
        return []


def atm_iv_for_expiration(client, symbol: str, expiration: str, spot: float) -> dict:
    """Return {dte, atm_call_mark, atm_put_mark, straddle, emove_pct, iv_pct}."""
    try:
        ch = client.get_option_chain(OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=InstrumentType.EQUITY),
            expiration_date=expiration,
        ))
    except Exception as e:
        return {"error": str(e)}
    calls = ch.calls or []
    puts = ch.puts or []
    if not calls or not puts:
        return {"error": "empty chain"}
    atm_call = min(calls, key=lambda c: abs(parse_osi_strike(c.instrument.symbol) - spot), default=None)
    atm_put = min(puts, key=lambda p: abs(parse_osi_strike(p.instrument.symbol) - spot), default=None)
    if not atm_call or not atm_put:
        return {"error": "no ATM strike"}
    cb = float(atm_call.bid) if atm_call.bid else 0
    ca = float(atm_call.ask) if atm_call.ask else 0
    pb = float(atm_put.bid) if atm_put.bid else 0
    pa = float(atm_put.ask) if atm_put.ask else 0
    call_mark = (cb + ca) / 2 if cb and ca else float(atm_call.last or 0)
    put_mark = (pb + pa) / 2 if pb and pa else float(atm_put.last or 0)
    if call_mark <= 0 or put_mark <= 0:
        return {"error": "no marks"}
    straddle = call_mark + put_mark
    emove_pct = (straddle / spot) * 100
    try:
        dte = (datetime.strptime(expiration, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return {"error": "bad date"}
    if dte <= 0:
        return {"error": "past expiration"}
    iv_pct = (straddle / spot) / np.sqrt(dte / 365.0) * 100
    return {
        "expiration": expiration,
        "dte": dte,
        "call_strike": parse_osi_strike(atm_call.instrument.symbol),
        "put_strike": parse_osi_strike(atm_put.instrument.symbol),
        "call_mark": call_mark,
        "put_mark": put_mark,
        "straddle": straddle,
        "emove_pct": round(emove_pct, 2),
        "iv_pct": round(iv_pct, 2),
    }


def classify_curve(points: list[dict]) -> str:
    """Classify the IV term structure shape."""
    if len(points) < 3:
        return "insufficient data"
    # Compare near (≤14 DTE) vs far (≥ max_dte/2)
    if not points:
        return "no data"
    max_dte = max(p["dte"] for p in points)
    far_threshold = max(30, max_dte // 2)
    short_ivs = [p["iv_pct"] for p in points if p["dte"] <= 14 and "iv_pct" in p]
    long_ivs = [p["iv_pct"] for p in points if p["dte"] >= far_threshold and "iv_pct" in p]
    if not short_ivs or not long_ivs:
        return f"mixed (short: {len(short_ivs)} pts, long: {len(long_ivs)} pts)"
    short_avg = np.mean(short_ivs)
    long_avg = np.mean(long_ivs)
    diff = short_avg - long_avg
    if abs(diff) < 1.0:
        return f"FLAT (near {short_avg:.1f}% vs far {long_avg:.1f}%)"
    if diff > 0:
        return f"BACKWARDATION (near {short_avg:.1f}% > far {long_avg:.1f}%, near-term fear)"
    return f"CONTANGO (near {short_avg:.1f}% < far {long_avg:.1f}%, far-term priced higher)"


def ascii_curve(points: list[dict], iv_min: float, iv_max: float, width: int = 50) -> str:
    """Render IV curve as ASCII horizontal bar chart."""
    if not points:
        return "  (no points)"
    if iv_max <= iv_min:
        iv_max = iv_min + 1
    lines = []
    for p in points:
        if "iv_pct" not in p:
            continue
        iv = p["iv_pct"]
        dte = p["dte"]
        bar_len = int(((iv - iv_min) / (iv_max - iv_min)) * width)
        bar = "█" * bar_len
        print(f"  {p['expiration']}  DTE={dte:>3}  IV={iv:>5.1f}%  {bar}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--max-expirations", type=int, default=10)
    ap.add_argument("--min-dte", type=int, default=1)
    ap.add_argument("--max-dte", type=int, default=120)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    symbol = args.ticker.upper()
    client = get_client()

    # Spot
    t = yf.Ticker(symbol)
    hist = t.history(period="5d", auto_adjust=True)
    if hist.empty:
        print(f"  ! no spot for {symbol}", file=sys.stderr)
        sys.exit(1)
    spot = float(hist["Close"].iloc[-1])

    # Expirations
    expirations = fetch_expirations(client, symbol)
    if not expirations:
        print(f"  ! no expirations for {symbol}", file=sys.stderr)
        sys.exit(1)

    # Filter by DTE window
    points = []
    for exp in expirations:
        try:
            d = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - date.today()).days
        if dte < args.min_dte or dte > args.max_dte:
            continue
        points.append({"expiration": exp, "dte": dte})

    points.sort(key=lambda x: x["dte"])
    # If we have many expirations, space them out across the DTE range
    if len(points) > args.max_expirations:
        # Take evenly-spaced indices to cover the curve
        step = len(points) / args.max_expirations
        points = [points[int(i * step)] for i in range(args.max_expirations)]

    # Fetch IV for each
    print(f"  Fetching ATM IV for {len(points)} expirations of {symbol}...", file=sys.stderr)
    iv_points = []
    for p in points:
        result = atm_iv_for_expiration(client, symbol, p["expiration"], spot)
        if "iv_pct" in result:
            iv_points.append(result)

    if args.json:
        print(json.dumps({
            "as_of": datetime.now().isoformat(),
            "ticker": symbol,
            "spot": spot,
            "expirations": iv_points,
            "classification": classify_curve(iv_points),
        }, indent=2))
        return

    print(f"\n{'#'*78}")
    print(f"# TERM STRUCTURE — {symbol} @ ${spot:.2f}  ({datetime.now().strftime('%Y-%m-%d %H:%M ET')})")
    print(f"{'#'*78}\n")

    if not iv_points:
        print("  No valid IV points.")
        return

    ivs = [p["iv_pct"] for p in iv_points]
    iv_min, iv_max = min(ivs), max(ivs)
    print(f"  IV range: {iv_min:.1f}% to {iv_max:.1f}%  (Δ {iv_max-iv_min:+.1f}%)")
    print(f"  Shape:    {classify_curve(iv_points)}")
    print()
    print(f"  {'Expiration':<12} {'DTE':>4}  {'Straddle':>9} {'EMove%':>7} {'IV%':>6}  Curve")
    print(f"  {'-'*12} {'-'*4}  {'-'*9} {'-'*7} {'-'*6}  {'-'*40}")
    for p in iv_points:
        bar_len = int(((p["iv_pct"] - iv_min) / max(1, iv_max - iv_min)) * 35)
        bar = "█" * bar_len
        print(f"  {p['expiration']:<12} {p['dte']:>4}  ${p['straddle']:>8.2f} "
              f"{p['emove_pct']:>6.1f}% {p['iv_pct']:>5.1f}%  {bar}")

    print()
    # Trading read
    near_iv = next((p["iv_pct"] for p in iv_points if 7 <= p["dte"] <= 21), None)
    far_iv = next((p["iv_pct"] for p in iv_points if 30 <= p["dte"] <= 60), None)
    if near_iv and far_iv:
        if near_iv > far_iv + 2:
            print(f"  → Near-term ({(near_iv - far_iv):.1f}% richer). Sell 7-21 DTE, avoid far-dated.")
        elif far_iv > near_iv + 2:
            print(f"  → Far-dated ({(far_iv - near_iv):.1f}% richer). Sell 45-60 DTE, avoid short-dated.")
        else:
            print(f"  → Curve roughly flat. Use 30-45 DTE sweet spot for CSP/CC.")


if __name__ == "__main__":
    main()
