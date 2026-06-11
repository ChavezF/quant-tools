#!/usr/bin/env python3.12
"""Small JSON cache helpers for API-heavy toolkit calls."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

from common import PROJECT_ROOT, atomic_write_json


T = TypeVar("T")
CACHE_DIR = PROJECT_ROOT / ".cache"


def cache_key(namespace: str, *parts: object) -> str:
    raw = json.dumps([namespace, *parts], sort_keys=True, default=str)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"{namespace}_{digest}.json"


def cache_path(namespace: str, *parts: object) -> Path:
    return CACHE_DIR / cache_key(namespace, *parts)


def read_cache(namespace: str, *parts: object, ttl_seconds: int) -> Any | None:
    path = cache_path(namespace, *parts)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    created_at = float(payload.get("created_at", 0) or 0)
    if ttl_seconds > 0 and time.time() - created_at > ttl_seconds:
        return None
    return payload.get("value")


def write_cache(namespace: str, value: Any, *parts: object) -> None:
    # Atomic so concurrent runs sharing the cache never read a partial file.
    atomic_write_json(cache_path(namespace, *parts), {"created_at": time.time(), "value": value})


def cached(namespace: str, ttl_seconds: int, fn: Callable[[], T], *parts: object) -> T:
    value = read_cache(namespace, *parts, ttl_seconds=ttl_seconds)
    if value is not None:
        return value
    value = fn()
    write_cache(namespace, value, *parts)
    return value
