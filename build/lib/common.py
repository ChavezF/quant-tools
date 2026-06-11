#!/usr/bin/env python3.12
"""Shared utilities for the quant toolkit command-line scripts."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SCRIPTS_DIR = Path(__file__).resolve().parent


def _project_root() -> Path:
    """Locate the directory that owns config.json, state/, and reports/.

    Running from a checkout, that's the repository root. Running from an
    installed package (pip install quant-tools), the module's parent is
    site-packages — state must NOT land there, so fall back to the working
    directory. QUANT_TOOLS_HOME overrides both.
    """
    env = os.environ.get("QUANT_TOOLS_HOME")
    if env:
        return Path(env).expanduser().resolve()
    checkout = SCRIPTS_DIR.parent
    if (checkout / "config.example.json").exists():
        return checkout
    return Path.cwd()


PROJECT_ROOT = _project_root()
STATE_DIR = PROJECT_ROOT / "state"


def atomic_write_json(path: Path | str, payload: Any) -> None:
    """Write JSON durably: temp file in the same directory, fsync, then rename.

    A crash mid-write can never leave a truncated file at `path` — readers see
    either the old content or the new content. Use this for every state file
    that the toolkit cannot afford to lose (journal, positions, cursors).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as handle:
            handle.write(json.dumps(payload, indent=2, default=str))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def state_lock(
    name: str = "state",
    timeout_seconds: float = 30.0,
    stale_seconds: float = 1800.0,
) -> Iterator[None]:
    """Advisory cross-process lock for state mutations (cron vs manual runs).

    Implemented with O_CREAT|O_EXCL so it works on every platform CI runs on.
    A lock older than `stale_seconds` is assumed abandoned (crashed process)
    and is broken with a warning. On contention past `timeout_seconds` the
    process exits loudly rather than silently interleaving writes.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / f".{name}.lock"
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} at={time.time()}".encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue  # holder released between open() and stat(); retry now
            if age > stale_seconds:
                print(
                    f"Warning: breaking stale lock {lock_path} (age {age:.0f}s)",
                    file=sys.stderr,
                )
                lock_path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                raise SystemExit(
                    f"Another quant-tools process holds {lock_path}. "
                    "Wait for it to finish (or delete the lock file if you are "
                    "sure no other run is active) and retry."
                )
            time.sleep(0.2)
    try:
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def read_json(path: Path | str | None, default: Any = None) -> Any:
    """Read a JSON file, returning `default` ({} unless overridden) when the
    path is unset, missing, or unparseable. Use for optional report inputs."""
    if default is None:
        default = {}
    if not path:
        return default
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return default


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


# Earliest occurrence of YYMMDD + C/P + 8-digit strike (thousandths of a dollar).
_OSI_PATTERN = re.compile(r"(?a)(\d{6})([CP])(\d{8})")


def parse_osi_parts(osi: str) -> dict[str, Any]:
    """Parse an OCC/OSI-style option symbol such as AAPL260116C00270000."""
    match = _OSI_PATTERN.search(osi)
    if not match:
        return {"underlying": osi, "expiration": "", "option_type": "", "strike": None}
    yy_mm_dd = match.group(1)
    return {
        "underlying": osi[: match.start()],
        "expiration": f"20{yy_mm_dd[:2]}-{yy_mm_dd[2:4]}-{yy_mm_dd[4:6]}",
        "option_type": match.group(2),
        "strike": int(match.group(3)) / 1000.0,
    }


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
