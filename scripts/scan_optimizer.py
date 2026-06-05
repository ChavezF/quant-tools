#!/usr/bin/env python3.12
"""Pure helpers for scan expiration and spread-width optimization."""
from __future__ import annotations

from datetime import date, datetime


def select_expirations(expirations: list[str], min_dte: int, max_dte: int,
                       max_expirations: int) -> list[tuple[str, int]]:
    """Return expirations in the DTE window, nearest to the target midpoint first."""
    target_dte = (min_dte + max_dte) // 2
    ranked = []
    for exp in expirations:
        try:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (exp_date - date.today()).days
        if min_dte <= dte <= max_dte:
            ranked.append((abs(dte - target_dte), exp, dte))
    ranked.sort(key=lambda row: (row[0], row[2]))
    return [(exp, dte) for _, exp, dte in ranked[:max(1, max_expirations)]]


def parse_wing_widths(values: list[float] | None) -> list[float]:
    widths = [float(v) for v in (values or [5.0]) if float(v) > 0]
    return sorted(set(widths)) or [5.0]
