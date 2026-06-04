#!/usr/bin/env python3.12
"""Shared utilities for the quant toolkit command-line scripts."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any


SCRIPTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS_DIR.parent
STATE_DIR = PROJECT_ROOT / "state"


def configure_public_imports() -> None:
    """Add the Public.com helper scripts directory to sys.path when available."""
    candidates = [
        os.environ.get("PUBLIC_IMPORTS_DIR"),
        "/home/chavez_f/.hermes/skills/openclaw-imports/public-dot-com/scripts",
    ]
    for raw_path in candidates:
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if path.exists():
            path_str = str(path)
            if path_str not in sys.path:
                sys.path.insert(0, path_str)
            return


def get_public_client():
    """Create an authenticated Public.com API client or exit with a clear error."""
    configure_public_imports()
    try:
        from config import get_account_id, get_api_secret
        from public_api_sdk import PublicApiClient, PublicApiClientConfiguration
        from public_api_sdk.auth_config import ApiKeyAuthConfig
    except ImportError as exc:
        print(
            "Error: Public.com SDK helpers are unavailable. "
            "Set PUBLIC_IMPORTS_DIR to the helper scripts directory.",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    secret = get_api_secret()
    if not secret:
        print("Error: PUBLIC_COM_SECRET missing.", file=sys.stderr)
        raise SystemExit(1)

    return PublicApiClient(
        ApiKeyAuthConfig(api_secret_key=secret),
        config=PublicApiClientConfiguration(default_account_number=get_account_id() or ""),
    )


def parse_osi_parts(osi: str) -> dict[str, Any]:
    """Parse an OCC/OSI-style option symbol such as AAPL260116C00270000."""
    for i in range(len(osi)):
        suffix = osi[i:]
        if len(suffix) < 15:
            continue
        yy_mm_dd = suffix[:6]
        option_type = suffix[6]
        strike_raw = suffix[7:15]
        if yy_mm_dd.isdigit() and option_type in ("C", "P") and strike_raw.isdigit():
            return {
                "underlying": osi[:i],
                "expiration": f"20{yy_mm_dd[:2]}-{yy_mm_dd[2:4]}-{yy_mm_dd[4:6]}",
                "option_type": option_type,
                "strike": int(strike_raw) / 1000.0,
            }
    return {"underlying": osi, "expiration": "", "option_type": "", "strike": None}


def parse_osi_strike(osi: str) -> float | None:
    return parse_osi_parts(osi).get("strike")


def parse_osi_expiration(osi: str) -> str:
    return str(parse_osi_parts(osi).get("expiration") or "")


def underlying_from_position(pos: dict[str, Any]) -> str:
    if pos.get("type") != "OPTION":
        return str(pos.get("symbol", ""))
    return str(parse_osi_parts(str(pos.get("symbol", ""))).get("underlying") or pos.get("symbol", ""))


def greeks_to_dict(greeks: Any) -> dict[str, float]:
    """Normalize a Public.com Greek object into serializable numeric fields."""
    return {
        "delta": float(greeks.delta) if getattr(greeks, "delta", None) is not None else 0.0,
        "gamma": float(greeks.gamma) if getattr(greeks, "gamma", None) is not None else 0.0,
        "theta": float(greeks.theta) if getattr(greeks, "theta", None) is not None else 0.0,
        "vega": float(greeks.vega) if getattr(greeks, "vega", None) is not None else 0.0,
        "iv": (
            float(greeks.implied_volatility)
            if getattr(greeks, "implied_volatility", None) is not None
            else 0.0
        ),
    }
