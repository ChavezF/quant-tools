#!/usr/bin/env python3.12
"""
daily_brief.py — Morning market intel brief for Telegram.

Pulls:
  - SPY/QQQ/IWM overnight and intraday change
  - VIX level
  - Top 3 options setups (CSP + CC) on SPY/QQQ
  - Top upcoming earnings (next 7 days)
  - 10Y yield, DXY, BTC, ETH

Outputs a compact text brief suitable for Telegram.

Usage:
  ./daily_brief.py --watchlist SPY QQQ IWM NVDA AAPL MSFT TSLA
  ./daily_brief.py --send                 # send to Telegram
  ./daily_brief.py --send --dry-run       # print what would be sent
"""
import argparse
import subprocess
from datetime import date, datetime
import sys
import yfinance as yf

from common import configure_public_imports

configure_public_imports()

from options_screener import (
    get_client, fetch_quote, fetch_option_expirations,
    fetch_chain_with_greeks, screen_csp, screen_cc,
    fetch_underlying_metrics,
)


def index_snapshot(ticker_symbol: str) -> dict:
    """Get prev close, last close, % change for an index/asset."""
    try:
        t = yf.Ticker(ticker_symbol)
        hist = t.history(period="5d", auto_adjust=True)
        if len(hist) < 2:
            return {}
        last = float(hist["Close"].iloc[-1])
        prev = float(hist["Close"].iloc[-2])
        chg = last - prev
        pct = (chg / prev) * 100
        return {"ticker": ticker_symbol, "last": last, "prev": prev, "chg": chg, "pct": pct}
    except Exception as e:
        return {"ticker": ticker_symbol, "error": str(e)}


def get_top_setups(client, watchlist: list, max_per_ticker: int = 1) -> list[dict]:
    """Get top CSP/CC setup across the watchlist (delta ~0.30, 30-45 DTE)."""
    results = []
    for sym in watchlist:
        sym = sym.upper()
        quote = fetch_quote(client, sym)
        spot = quote.get("last") or quote.get("bid")
        if not spot:
            m = fetch_underlying_metrics(sym)
            spot = m.get("last_close")
        if not spot:
            continue
        exps = fetch_option_expirations(client, sym)
        if not exps:
            continue
        # Find best expiration 30-45 DTE
        target_dte = 35
        best_exp, best_dte, best_diff = None, None, 9999
        for exp in exps:
            try:
                d = datetime.strptime(exp, "%Y-%m-%d").date()
                dte = (d - date.today()).days
            except ValueError:
                continue
            if 28 <= dte <= 50 and abs(dte - target_dte) < best_diff:
                best_exp, best_dte, best_diff = exp, dte, abs(dte - target_dte)
        if not best_exp:
            continue
        chain = fetch_chain_with_greeks(client, sym, best_exp, spot, max_legs=40)
        csp = screen_csp(chain, spot, best_dte, target_delta=-0.30, min_oi=20)[:max_per_ticker]
        cc = screen_cc(chain, spot, best_dte, target_delta=0.30, min_oi=20)[:max_per_ticker]
        for r in csp:
            r["ticker"] = sym
            r["expiration"] = best_exp
            results.append(r)
        for r in cc:
            r["ticker"] = sym
            r["expiration"] = best_exp
            results.append(r)
    # Sort by ann_roc
    results.sort(key=lambda r: r.get("ann_roc_pct", 0), reverse=True)
    return results[:5]


def get_upcoming_earnings(watchlist: list, days: int = 14) -> list[dict]:
    """Quick earnings list — no API calls needed, just yfinance."""
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
            if 0 <= dte <= days:
                out.append({"ticker": sym, "date": str(ed), "days": dte})
        except Exception as e:
            print(f"  ! {sym} earnings lookup failed: {e}", file=sys.stderr)
            continue
    out.sort(key=lambda x: x["days"])
    return out


def build_brief(watchlist: list) -> str:
    """Build the brief text — Telegram-friendly (no markdown tables, monospace ok)."""
    lines = []
    lines.append(f"☀️ MORNING BRIEF — {datetime.now().strftime('%a %b %d, %H:%M ET')}")
    lines.append("")

    # Macro regime (top of brief — most important for sizing)
    lines.append("🎯 MACRO REGIME")
    try:
        from macro_overlay import (
            get_vix_regime, get_yield_curve, get_dxy_trend,
            get_upcoming_earnings, compute_regime_score,
        )
        vix = get_vix_regime()
        yc = get_yield_curve()
        dxy = get_dxy_trend()
        earnings = get_upcoming_earnings(watchlist, days=7)
        regime = compute_regime_score(vix, yc, dxy, earnings)
        score = regime["score"]
        bar = "█" * int(score / 5) + "░" * (20 - int(score / 5))
        lines.append(f"  Score: {score}/100  {bar}")
        lines.append(f"  Verdict: {regime['verdict']}")
        # Top 3 reasons
        for r in regime["reasons"][:3]:
            lines.append(f"   • {r}")
        # Earnings in next 7d
        if earnings:
            lines.append(f"  📅 Earnings 7d: " + ", ".join(f"{e['ticker']}({e['days']}d)" for e in earnings[:5]))
        else:
            lines.append(f"  📅 Earnings 7d: clear (no watchlist events)")
    except Exception as e:
        lines.append(f"  (macro fetch failed: {e})")
    lines.append("")

    # Market snapshot
    lines.append("📈 MARKETS")
    for sym in ("SPY", "QQQ", "IWM", "^VIX", "^TNX", "DX-Y.NYB", "BTC-USD", "ETH-USD"):
        s = index_snapshot(sym)
        if "error" in s or "last" not in s:
            continue
        chg_sign = "+" if s["chg"] >= 0 else ""
        emoji = "🟢" if s["chg"] >= 0 else "🔴"
        if sym == "^VIX":
            lines.append(f"  {emoji} VIX       {s['last']:>7.2f}  {chg_sign}{s['pct']:>5.2f}%")
        elif sym == "^TNX":
            lines.append(f"  {emoji} 10Y Yld   {s['last']:>7.3f}% {chg_sign}{s['pct']:>5.2f}%")
        elif sym == "DX-Y.NYB":
            lines.append(f"  {emoji} DXY       {s['last']:>7.2f}  {chg_sign}{s['pct']:>5.2f}%")
        elif sym in ("BTC-USD", "ETH-USD"):
            name = "BTC" if "BTC" in sym else "ETH"
            lines.append(f"  {emoji} {name:<8} ${s['last']:>8,.0f}  {chg_sign}{s['pct']:>5.2f}%")
        else:
            lines.append(f"  {emoji} {sym:<8} ${s['last']:>8,.2f}  {chg_sign}{s['pct']:>5.2f}%")

    # IV regime (lazy import to avoid circular)
    lines.append("")
    lines.append("🎯 IV REGIME")
    try:
        from iv_rank import get_current_iv, get_historical_rv_series, rank_metrics, classify_iv_regime
        iv_client = get_client()
        for sym in ("SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMD"):
            t = yf.Ticker(sym)
            hist = t.history(period="5d", auto_adjust=True)
            if hist.empty:
                continue
            spot = float(hist["Close"].iloc[-1])
            iv = get_current_iv(iv_client, sym, spot)
            rv_series = get_historical_rv_series(sym)
            if iv <= 0 or not rv_series:
                continue
            metrics = rank_metrics(iv, rv_series)
            if metrics["rank"] is None:
                continue
            regime = classify_iv_regime(metrics["rank"])
            arrow = "🟢" if metrics["rank"] >= 50 else "🟡" if metrics["rank"] >= 25 else "🔴"
            lines.append(f"  {arrow} {sym:<5} IV:{iv:>5.1f}%  IVRank:{metrics['rank']:>4.0f}  {regime}")
    except Exception as e:
        lines.append(f"  (IV rank fetch failed: {e})")

    # Top setups
    lines.append("")
    lines.append("💰 TOP OPTIONS SETUPS (Δ≈0.30, 30-50DTE)")
    try:
        client = get_client()
        setups = get_top_setups(client, watchlist)
        if not setups:
            lines.append("  (no live setups — check API)")
        for s in setups:
            strat = s["strategy"]
            strike = s.get("strike") or f"{s.get('short_strike')}/{s.get('long_strike')}"
            pop = s.get("pop_pct", 0)
            ann = s.get("ann_roc_pct", 0)
            credit = s.get("credit", 0)
            iv = s.get("iv_pct", 0) or 0
            osi = s.get("osi", "")
            tag = "CSP" if strat == "CSP" else "CC " if strat == "CC" else "BPS"
            lines.append(f"  [{tag}] {s['ticker']:<5} ${strike:<10} "
                        f"${credit:>5.2f}  POP:{pop:>4.0f}%  ROC:{ann:>4.1f}%/yr  IV:{iv:.0f}%")
            if osi:
                lines.append(f"        OSI: {osi}")
    except Exception as e:
        lines.append(f"  (error fetching setups: {e})")

    # Upcoming earnings
    lines.append("")
    lines.append("📅 EARNINGS (next 14d)")
    earnings = get_upcoming_earnings(watchlist, days=14)
    if not earnings:
        lines.append("  (none in next 14d)")
    for e in earnings[:8]:
        urgency = "🚨" if e["days"] <= 3 else "  "
        lines.append(f"  {urgency} {e['ticker']:<6} {e['date']} ({e['days']}d)")

    # Footer
    lines.append("")
    lines.append(f"📊 Run `screener` or `earnings-scan` for detail")

    return "\n".join(lines)


def send_telegram(message: str) -> bool:
    """Send via Hermes `hermes send` to Telegram home channel."""
    try:
        result = subprocess.run(
            ["hermes", "send", "--to", "telegram", message],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            print(f"hermes send stderr: {result.stderr}", file=sys.stderr)
        return result.returncode == 0
    except Exception as e:
        print(f"Telegram send failed: {e}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchlist", nargs="+",
                    default=["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD"])
    ap.add_argument("--send", action="store_true", help="Send to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="Print only, don't send")
    args = ap.parse_args()

    brief = build_brief(args.watchlist)
    print(brief)

    if args.dry_run:
        print("\n--- DRY RUN (would send to Telegram) ---", file=sys.stderr)
    elif args.send:
        ok = send_telegram(brief)
        if ok:
            print("\n--- Sent to Telegram ---", file=sys.stderr)
        else:
            print("\n--- Telegram send FAILED — brief printed above ---", file=sys.stderr)


if __name__ == "__main__":
    main()
