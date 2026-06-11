#!/usr/bin/env python3.12
"""Reliability helpers for live data calls."""
from __future__ import annotations

import random
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, TypeVar


T = TypeVar("T")


@dataclass
class CallMeta:
    source: str
    ok: bool
    attempts: int
    elapsed_ms: int
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def retry_call(
    fn: Callable[[], T],
    *,
    source: str,
    retries: int = 2,
    base_delay: float = 0.25,
    backoff: float = 2.0,
    jitter: float = 0.05,
) -> tuple[T | None, CallMeta]:
    """Run a live data call with bounded retry/backoff and timing metadata."""
    start = time.perf_counter()
    attempts = 0
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        attempts = attempt + 1
        try:
            value = fn()
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            return value, CallMeta(source=source, ok=True, attempts=attempts, elapsed_ms=elapsed_ms)
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = base_delay * (backoff ** attempt)
            if jitter > 0:
                delay += random.uniform(0, jitter)
            time.sleep(delay)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    return None, CallMeta(
        source=source,
        ok=False,
        attempts=attempts,
        elapsed_ms=elapsed_ms,
        error=str(last_error) if last_error else "unknown error",
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quote_issues(
    quote: dict,
    reference_price: float | None = None,
    max_reference_divergence_pct: float = 10.0,
) -> list[str]:
    """Sanity-check an underlying quote before it drives a scan.

    A bad spot poisons every candidate priced from it, so reject obviously
    broken data (non-positive last, negative or crossed bid/ask) and flag
    softer problems (staleness, divergence from an independent reference
    close) for the caller to surface.
    """
    issues = []
    last = quote.get("last")
    bid = quote.get("bid")
    ask = quote.get("ask")
    if last is not None and float(last) <= 0:
        issues.append("non-positive last")
    if bid is not None and float(bid) < 0:
        issues.append("negative bid")
    if ask is not None and float(ask) < 0:
        issues.append("negative ask")
    if bid and ask and float(bid) > float(ask):
        issues.append("crossed market (bid > ask)")
    if quote.get("stale"):
        issues.append("stale quote")
    if reference_price and last and float(reference_price) > 0:
        divergence = abs(float(last) - float(reference_price)) / float(reference_price) * 100
        if divergence > max_reference_divergence_pct:
            issues.append(
                f"last diverges {divergence:.1f}% from reference close {float(reference_price):.2f}"
            )
    return issues


HARD_QUOTE_ISSUES = ("non-positive", "negative", "crossed")


def hard_quote_issues(issues: list[str]) -> list[str]:
    """Issues that make the quote unusable (vs warnings worth surfacing)."""
    return [issue for issue in issues if issue.startswith(HARD_QUOTE_ISSUES)]


def option_leg_issues(bid: float, ask: float, iv: float | None = None) -> list[str]:
    """Data-quality issues for a single option leg quote.

    `iv` is in decimal terms (0.30 = 30%); anything outside (0, 5] is treated
    as a feed glitch rather than a 500%+ vol regime.
    """
    issues = []
    if bid < 0 or ask < 0:
        issues.append("negative quote")
    if ask > 0 and bid > ask:
        issues.append("crossed market")
    if iv is not None and not (0 < iv <= 5):
        issues.append("implausible IV")
    return issues


def quote_is_stale(as_of_iso: str | None, max_age_seconds: int) -> bool:
    if not as_of_iso:
        return True
    try:
        as_of = datetime.fromisoformat(as_of_iso)
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    age = (datetime.now(timezone.utc) - as_of.astimezone(timezone.utc)).total_seconds()
    return age > max_age_seconds
