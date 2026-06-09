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


def derive_live_account_nav(portfolio_report: dict[str, Any] | None, default: float) -> float:
    """Pick the live account NAV from a `portfolio_risk --json` report.

    Prefers `portfolio.options_bp` (the buying-power figure the broker actually
    quotes for new option orders), then `portfolio.cash_only` (settled cash
    without margin), then `portfolio.buying_power` (which on Reg-T margin
    accounts is 2× cash and would over-state the cash budget). Falls back to
    the config default when the report is missing or in demo mode (the demo
    report has `positions` with synthetic 100-share lots, but the BP fields
    are absent in that case so the fallback is correct automatically).

    This replaces the legacy `--account-nav` CLI flag plumbing: callers that
    consume a `portfolio_report` should derive NAV from here so deposits /
    withdrawals / gains / losses flow through automatically without code
    changes.
    """
    if not portfolio_report:
        return default
    portfolio = portfolio_report.get("portfolio", {}) or {}
    for key in ("options_bp", "cash_only", "buying_power"):
        value = portfolio.get(key)
        if value is not None and float(value) > 0:
            return float(value)
    return default


# Map the macro regime verdict emitted by daily_brief.py to the --sizing-mode
# flag. The macro scoring thresholds (macro_overlay.build_overlay) are:
#   score >= 65 → AGGRESSIVE  (scale up short premium)
#   score >= 50 → FAVORABLE   (normal sizing)
#   score >= 35 → CAUTIOUS    (half size, skip earnings names)
#   else        → DEFENSIVE   (cash > premium, wait for setup)
# AGGRESSIVE/FAVORABLE pass the "macro > FAVORABLE" guard from the standing
# rule ("half size until macro regime > FAVORABLE or portfolio IVRank > 50"),
# so we go full-size. CAUTIOUS/DEFENSIVE are below the bar → half size. Any
# parse failure or unknown verdict falls back to cautious (the safer default).
REGIME_TO_SIZING = {
    "AGGRESSIVE": "aggressive",
    "FAVORABLE": "normal",
    "CAUTIOUS": "cautious",
    "DEFENSIVE": "cautious",
}


def parse_regime_from_brief(brief_text: str) -> str | None:
    """Pull the macro regime verdict from daily_brief.py output.

    Looks for the line `  Verdict: <VERDICT>: <reasoning>` and returns the
    verdict token (AGGRESSIVE/FAVORABLE/CAUTIOUS/DEFENSIVE). Returns None
    when the line is missing or malformed — caller falls back to cautious.
    """
    for line in brief_text.splitlines():
        if "Verdict:" not in line:
            continue
        after = line.split("Verdict:", 1)[1].strip()
        if not after:
            return None
        return after.split(":", 1)[0].strip().upper() or None
    return None


def derive_sizing_mode(brief_text: str) -> tuple[str, str | None]:
    """Map the brief's regime to --sizing-mode. Returns (mode, verdict) tuple.

    verdict is None if the parse failed (caller logs a warning). Mode always
    resolves to one of cautious/normal/aggressive — unknown verdicts fall
    through to cautious rather than raising, so a brief-format drift can't
    crash the morning cron."""
    verdict = parse_regime_from_brief(brief_text)
    if verdict is None:
        return "cautious", None
    return REGIME_TO_SIZING.get(verdict, "cautious"), verdict
