#!/usr/bin/env python3.12
"""Discover promising symbols to feed into the options scanner."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any

from cache_utils import cached
from candidate_scoring import clamp, score_peak, score_range
from toolkit_config import add_config_argument, load_config


def score_discovery_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    price = float(metrics.get("price") or 0)
    avg_volume = float(metrics.get("avg_volume") or 0)
    rv_21d_pct = float(metrics.get("rv_21d_pct") or 0)
    trend_3m_pct = float(metrics.get("trend_3m_pct") or 0)
    earnings_days = metrics.get("days_to_earnings")

    liquidity = score_range(avg_volume, 500_000, 8_000_000)
    volatility = score_peak(rv_21d_pct, target=35, tolerance=30)
    trend = score_peak(trend_3m_pct, target=8, tolerance=25)
    price_score = score_range(price, 20, 150)

    if earnings_days is None:
        event_score = 85.0
    elif earnings_days <= 5:
        event_score = 25.0
    elif earnings_days <= 14:
        event_score = 55.0
    else:
        event_score = 85.0

    score = (
        liquidity * 0.30
        + volatility * 0.25
        + trend * 0.18
        + event_score * 0.17
        + price_score * 0.10
    )
    return {
        "discovery_score": round(clamp(score), 1),
        "components": {
            "liquidity": round(liquidity, 1),
            "volatility": round(volatility, 1),
            "trend": round(trend, 1),
            "event": round(event_score, 1),
            "price": round(price_score, 1),
        },
    }


def next_earnings_days(ticker) -> int | None:
    try:
        edf = ticker.earnings_dates
        if edf is None or edf.empty:
            return None
        today = date.today()
        for d in edf.index:
            d2 = d.date() if hasattr(d, "date") else d
            if d2 >= today:
                return (d2 - today).days
    except Exception:
        return None
    return None


def fetch_symbol_metrics(symbol: str) -> dict[str, Any]:
    import numpy as np
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    hist = ticker.history(period="6mo", auto_adjust=True)
    if hist.empty or len(hist) < 30:
        return {"symbol": symbol, "error": "insufficient history"}
    closes = hist["Close"].dropna()
    volumes = hist["Volume"].dropna() if "Volume" in hist else []
    log_returns = np.log(closes / closes.shift(1)).dropna()
    price = float(closes.iloc[-1])
    rv_21d = float(log_returns.tail(21).std() * np.sqrt(252) * 100)
    trend_3m = float((closes.iloc[-1] / closes.iloc[-63] - 1) * 100) if len(closes) >= 63 else 0.0
    avg_volume = float(volumes.tail(30).mean()) if len(volumes) else 0.0
    return {
        "symbol": symbol,
        "price": round(price, 2),
        "avg_volume": round(avg_volume, 0),
        "rv_21d_pct": round(rv_21d, 2),
        "trend_3m_pct": round(trend_3m, 2),
        "days_to_earnings": next_earnings_days(ticker),
    }


def discover(
    symbols: list[str],
    *,
    min_price: float,
    min_avg_volume: float,
    top: int,
    ttl_seconds: int = 900,
) -> list[dict[str, Any]]:
    rows = []
    for symbol in symbols:
        symbol = symbol.upper()
        try:
            metrics = cached(
                "discovery_metrics",
                ttl_seconds,
                lambda s=symbol: fetch_symbol_metrics(s),
                symbol,
                "6mo",
            )
        except Exception as exc:
            print(f"  ! discovery failed for {symbol}: {exc}", file=sys.stderr)
            continue
        if metrics.get("error"):
            continue
        if float(metrics.get("price") or 0) < min_price:
            continue
        if float(metrics.get("avg_volume") or 0) < min_avg_volume:
            continue
        scored = {**metrics, **score_discovery_metrics(metrics)}
        rows.append(scored)
    rows.sort(key=lambda row: row["discovery_score"], reverse=True)
    return rows[:top]


def print_discovery(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'#'*78}")
    print("# OPPORTUNITY DISCOVERY")
    print(f"{'#'*78}\n")
    print(f"  {'Score':>5} {'Symbol':<6} {'Price':>8} {'Vol30':>12} {'RV21':>7} {'Trend3m':>8} {'Earn':>5}")
    for row in rows:
        earn = row.get("days_to_earnings")
        earn_s = str(earn) if earn is not None else "-"
        print(
            f"  {row['discovery_score']:>5.1f} {row['symbol']:<6} ${row['price']:>7.2f} "
            f"{row['avg_volume']:>12,.0f} {row['rv_21d_pct']:>6.1f}% "
            f"{row['trend_3m_pct']:>+7.1f}% {earn_s:>5}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    ap.add_argument("--symbols", nargs="+")
    ap.add_argument("--watchlist-name", default="discovery")
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--min-avg-volume", type=float)
    ap.add_argument("--top", type=int)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    cfg = load_config(args.config)
    disc_cfg = cfg.get("discovery", {})
    cache_cfg = cfg.get("cache", {})
    symbols = args.symbols or cfg["watchlists"].get(args.watchlist_name)
    if not symbols:
        raise SystemExit(f"Unknown watchlist: {args.watchlist_name}")

    rows = discover(
        symbols,
        min_price=args.min_price if args.min_price is not None else float(disc_cfg.get("min_price", 20)),
        min_avg_volume=args.min_avg_volume if args.min_avg_volume is not None else float(disc_cfg.get("min_avg_volume", 2_000_000)),
        top=args.top if args.top is not None else int(disc_cfg.get("top", 20)),
        ttl_seconds=int(cache_cfg.get("underlying_metrics_ttl_seconds", 900)),
    )
    if args.json:
        print(json.dumps({"results": rows}, indent=2, default=str))
        return
    print_discovery(rows)


if __name__ == "__main__":
    main()
