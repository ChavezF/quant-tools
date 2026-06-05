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
