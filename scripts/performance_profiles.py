#!/usr/bin/env python3.12
"""Derived live-performance profiles from the trade journal."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from trade_journal import DEFAULT_STATE_FILE, load_state


def closed_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [trade for trade in trades if trade.get("status") == "CLOSED"]


def empty_bucket() -> dict[str, Any]:
    return {
        "count": 0,
        "wins": 0,
        "losses": 0,
        "pnl": 0.0,
        "gross_wins": 0.0,
        "gross_losses": 0.0,
        "avg_pnl": 0.0,
        "win_rate": 0.0,
        "profit_factor": None,
        "confidence": "NONE",
    }


def add_to_bucket(bucket: dict[str, Any], trade: dict[str, Any]) -> None:
    pnl = float(trade.get("realized_pnl") or 0)
    bucket["count"] += 1
    bucket["pnl"] += pnl
    if pnl > 0:
        bucket["wins"] += 1
        bucket["gross_wins"] += pnl
    else:
        bucket["losses"] += 1
        bucket["gross_losses"] += abs(pnl)


def finalize_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    count = int(bucket["count"])
    bucket["pnl"] = round(float(bucket["pnl"]), 2)
    bucket["gross_wins"] = round(float(bucket["gross_wins"]), 2)
    bucket["gross_losses"] = round(float(bucket["gross_losses"]), 2)
    bucket["avg_pnl"] = round(bucket["pnl"] / count, 2) if count else 0.0
    bucket["win_rate"] = round(bucket["wins"] / count * 100, 1) if count else 0.0
    bucket["profit_factor"] = (
        round(bucket["gross_wins"] / bucket["gross_losses"], 2)
        if bucket["gross_losses"] > 0
        else None
    )
    if count >= 20:
        bucket["confidence"] = "HIGH"
    elif count >= 8:
        bucket["confidence"] = "MEDIUM"
    elif count >= 3:
        bucket["confidence"] = "LOW"
    else:
        bucket["confidence"] = "TINY"
    return bucket


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
    strategy = defaultdict(empty_bucket)
    ticker = defaultdict(empty_bucket)
    ticker_strategy = defaultdict(empty_bucket)

    for trade in closed_trades(trades):
        ticker_key = str(trade.get("ticker") or "UNKNOWN").upper()
        strategy_key = str(trade.get("strategy") or "UNKNOWN").upper()
        combo_key = f"{ticker_key}|{strategy_key}"
        add_to_bucket(strategy[strategy_key], trade)
        add_to_bucket(ticker[ticker_key], trade)
        add_to_bucket(ticker_strategy[combo_key], trade)

    profiles = {
        "strategy": {key: finalize_bucket(value) for key, value in sorted(strategy.items())},
        "ticker": {key: finalize_bucket(value) for key, value in sorted(ticker.items())},
        "ticker_strategy": {key: finalize_bucket(value) for key, value in sorted(ticker_strategy.items())},
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
    ap.add_argument("--section", choices=["strategy", "ticker", "ticker_strategy"], default="ticker_strategy")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    state = load_state(Path(args.state_file))
    profiles = build_profiles(state.get("trades", []))
    if args.json:
        print(json.dumps(profiles, indent=2, default=str))
        return
    print_profiles(profiles, args.section)


if __name__ == "__main__":
    main()
