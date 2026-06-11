#!/usr/bin/env python3.12
"""Derived live-performance profiles from the trade journal."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_journal
from trade_stats import closed_trades, pnl_breakdown, profit_factor, win_rate_pct


def build_bucket(trades: list[dict[str, Any]]) -> dict[str, Any]:
    breakdown = pnl_breakdown(trades)
    count = breakdown["count"]
    pnl = round(breakdown["total_pnl"], 2)
    if count >= 20:
        confidence = "HIGH"
    elif count >= 8:
        confidence = "MEDIUM"
    elif count >= 3:
        confidence = "LOW"
    else:
        confidence = "TINY"
    return {
        "count": count,
        "wins": breakdown["wins"],
        "losses": breakdown["losses"],
        "pnl": pnl,
        "gross_wins": round(breakdown["gross_wins"], 2),
        "gross_losses": round(breakdown["gross_losses"], 2),
        "avg_pnl": round(pnl / count, 2) if count else 0.0,
        "win_rate": win_rate_pct(breakdown["wins"], count),
        "profit_factor": profit_factor(breakdown["gross_wins"], breakdown["gross_losses"]),
        "confidence": confidence,
    }


def profile_signal(profile: dict[str, Any] | None) -> str:
    if not profile or profile.get("count", 0) == 0:
        return "NO_HISTORY"
    count = int(profile["count"])
    win_rate = float(profile["win_rate"])
    pnl = float(profile["pnl"])
    avg_pnl = float(profile["avg_pnl"])
    if count < 3:
        return "INSUFFICIENT"
    if pnl > 0 and win_rate >= 60 and avg_pnl > 0:
        return "BOOST"
    if pnl < 0 or win_rate < 45:
        return "THROTTLE"
    return "NEUTRAL"


def build_profiles(trades: list[dict[str, Any]]) -> dict[str, Any]:
    strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ticker: dict[str, list[dict[str, Any]]] = defaultdict(list)
    ticker_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for trade in closed_trades(trades):
        ticker_key = str(trade.get("ticker") or "UNKNOWN").upper()
        strategy_key = str(trade.get("strategy") or "UNKNOWN").upper()
        strategy[strategy_key].append(trade)
        ticker[ticker_key].append(trade)
        ticker_strategy[f"{ticker_key}|{strategy_key}"].append(trade)

    profiles = {
        "strategy": {key: build_bucket(rows) for key, rows in sorted(strategy.items())},
        "ticker": {key: build_bucket(rows) for key, rows in sorted(ticker.items())},
        "ticker_strategy": {key: build_bucket(rows) for key, rows in sorted(ticker_strategy.items())},
    }
    for section in profiles.values():
        for profile in section.values():
            profile["signal"] = profile_signal(profile)
    return profiles


def lookup_profile(profiles: dict[str, Any], ticker: str, strategy: str) -> tuple[str, dict[str, Any] | None]:
    ticker_key = ticker.upper()
    strategy_key = strategy.upper()
    combo = profiles.get("ticker_strategy", {}).get(f"{ticker_key}|{strategy_key}")
    if combo and combo.get("count", 0) >= 3:
        return "ticker_strategy", combo
    strat = profiles.get("strategy", {}).get(strategy_key)
    if strat and strat.get("count", 0) >= 3:
        return "strategy", strat
    tick = profiles.get("ticker", {}).get(ticker_key)
    if tick and tick.get("count", 0) >= 3:
        return "ticker", tick
    return "none", None


def profile_note(scope: str, profile: dict[str, Any] | None) -> str:
    if not profile:
        return "no live profile"
    return (
        f"{scope}: {profile['signal'].lower()} n={profile['count']} "
        f"win={profile['win_rate']:.1f}% avg=${profile['avg_pnl']:,.0f} pnl=${profile['pnl']:,.0f}"
    )


def print_profiles(profiles: dict[str, Any], section: str) -> None:
    rows = profiles.get(section, {})
    print(f"\n{'#'*78}")
    print(f"# PERFORMANCE PROFILES — {section.upper()}")
    print(f"{'#'*78}\n")
    print(f"  {'Key':<18} {'Signal':<12} {'N':>3} {'Win%':>6} {'AvgPnL':>9} {'PnL':>10} {'PF':>6} {'Conf':<6}")
    for key, row in rows.items():
        pf = row["profit_factor"] if row["profit_factor"] is not None else "inf"
        print(
            f"  {key:<18} {row['signal']:<12} {row['count']:>3} {row['win_rate']:>5.1f}% "
            f"${row['avg_pnl']:>8,.2f} ${row['pnl']:>9,.2f} {str(pf):>6} {row['confidence']:<6}"
        )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    ap.add_argument("--db", help="Optional SQLite database; authoritative over the JSON journal when set")
    ap.add_argument("--section", choices=["strategy", "ticker", "ticker_strategy"], default="ticker_strategy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    state = load_journal(Path(args.state_file), args.db)
    profiles = build_profiles(state.get("trades", []))
    if args.json:
        print(json.dumps(profiles, indent=2, default=str))
        return
    print_profiles(profiles, args.section)


if __name__ == "__main__":
    main()
