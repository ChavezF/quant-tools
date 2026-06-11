#!/usr/bin/env python3.12
"""Configuration loading for quant-tools."""
from __future__ import annotations

import argparse
import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT


DEFAULT_CONFIG: dict[str, Any] = {
    "watchlists": {
        "core": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD"],
        "earnings": ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL"],
        "discovery": ["SPY", "QQQ", "IWM", "DIA", "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META"],
    },
    "scan": {
        "strategies": ["csp", "cc"],
        "min_dte": 14,
        "max_dte": 45,
        "target_delta": 0.30,
        "min_oi": 50,
        "max_expirations": 1,
        "wing_widths": [5.0],
    },
    "risk_limits": {
        "account_nav": 30000.0,
        "max_trade_risk_pct": 0.05,
        "max_trade_bp_pct": 0.20,
        "max_single_ticker_pct": 0.25,
        "max_portfolio_delta_abs": 250.0,
        "min_score": 55.0,
        "min_liquidity_score": 45.0,
        "min_pop_pct": 55.0,
    },
    "journal": {
        "path": "state/trades.json",
        "mark_to_market": True,
    },
    "cache": {
        "enabled": True,
        "underlying_metrics_ttl_seconds": 900,
    },
    "alerts": {
        "min_score": 68.0,
        "profit_target_pct": 50.0,
        "dte_warning": 21,
    },
    "data_reliability": {
        "retries": 2,
        "base_delay_seconds": 0.25,
        "quote_max_age_seconds": 900,
    },
    "discovery": {
        "watchlist_name": "discovery",
        "min_price": 20.0,
        "min_avg_volume": 2_000_000,
        "top": 20,
    },
    "correlation_groups": {
        "index_beta": ["SPY", "QQQ", "IWM", "DIA"],
        "mega_cap_tech": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "NFLX"],
        "energy": ["XOM", "CVX"],
        "banks": ["JPM", "GS", "BAC"],
    },
    "adaptive_sizing": {
        "min_trades": 5,
        "min_multiplier": 0.25,
        "max_multiplier": 1.15,
        "drawdown_limit_pct": 8.0,
    },
    "feedback": {
        "min_samples": 5,
    },
    "storage": {
        "enabled": True,
        "path": "state/quant_tools.db",
        "broker_snapshot": None,
    },
    "public_ingestion": {
        "enabled": False,
        "cursor_path": "state/public_fill_cursor.json",
        "snapshot_path": "state/public_broker_snapshot.json",
        "page_size": 100,
        "overlap_minutes": 15,
        "max_pages": 100,
    },
    "execution_lifecycle": {
        "pending_expiry_hours": 24,
        "partial_review_hours": 4,
        "suppress_duplicate_tickets": True,
    },
    "operations": {
        "backup_on_operator": True,
        "backup_dir": "state/backups",
        "backup_retention_days": 30,
        "backup_keep_last": 14,
        "health_check_on_operator": True,
    },
    "scenario_stress": {
        "enabled": True,
        "scenarios_path": None,
    },
    "portfolio_allocation": {
        "enabled": True,
        "max_positions": 6,
        "max_total_capital_pct": 0.35,
        "max_tail_loss_pct": 0.08,
        "max_expected_shortfall_pct": 0.08,
        "max_ticker_capital_pct": 0.15,
        "max_group_exposure_pct": 0.35,
        "stress_loss_fraction": 0.65,
        "include_reduce": True,
    },
    "validation": {
        "enabled": True,
        "min_train": 10,
        "test_window": 5,
        "thresholds": [50, 55, 60, 65, 70, 75],
        "min_selected": 3,
    },
    "drift_monitor": {
        "enabled": True,
        "recent_window": 10,
        "min_baseline": 10,
    },
    "position_management": {
        "enabled": True,
        "profit_target_pct": 50.0,
        "stop_loss_pct": 200.0,
        "manage_dte": 21,
        "urgent_dte": 7,
    },
}


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def config_path(raw_path: str | None = None) -> Path:
    raw_path = raw_path or os.environ.get("QUANT_CONFIG")
    if raw_path:
        path = Path(raw_path)
        return path if path.is_absolute() else PROJECT_ROOT / path
    default_path = PROJECT_ROOT / "config.json"
    if default_path.exists():
        return default_path
    return PROJECT_ROOT / "config.example.json"


def load_config(raw_path: str | None = None) -> dict[str, Any]:
    path = config_path(raw_path)
    if not path.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid config JSON: {path} ({exc})") from exc
    return deep_merge(DEFAULT_CONFIG, loaded)


def resolve_project_path(raw_path: str | None) -> str | None:
    if not raw_path:
        return None
    path = Path(raw_path)
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


def add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Path to config JSON; defaults to config.json, then config.example.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    add_config_argument(ap)
    args = ap.parse_args()
    print(json.dumps(load_config(args.config), indent=2))


if __name__ == "__main__":
    main()
