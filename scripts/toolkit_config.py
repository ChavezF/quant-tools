#!/usr/bin/env python3.12
"""Configuration loading for quant-tools."""
from __future__ import annotations

import argparse
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from common import PROJECT_ROOT


DEFAULT_CONFIG: dict[str, Any] = {
    "watchlists": {
        "core": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "AMD"],
        "earnings": ["NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META", "GOOGL"],
    },
    "scan": {
        "strategies": ["csp", "cc"],
        "min_dte": 14,
        "max_dte": 45,
        "target_delta": 0.30,
        "min_oi": 50,
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
    },
    "cache": {
        "enabled": True,
        "underlying_metrics_ttl_seconds": 900,
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
