"""Pure math utilities for fleet calculations."""

from __future__ import annotations


def clamp(value: float, low: float, high: float) -> float:
    """Return *value* bounded to the inclusive range [low, high].

    Args:
        value: The numeric value to bound.
        low: The lower bound (inclusive).
        high: The upper bound (inclusive).

    Returns:
        *low* if *value* is below it, *high* if above it, otherwise *value*.
    """
    if value < low:
        return low
    if value > high:
        return high
    return value


def percent(part: float, whole: float) -> float:
    """Return the percentage of *part* relative to *whole*, to 1 decimal place.

    Args:
        part: The portion value.
        whole: The total value. If 0, returns 0.0 instead of raising.

    Returns:
        ``round(100.0 * part / whole, 1)`` on success, or ``0.0`` when
        *whole* is zero.
    """
    if whole == 0:
        return 0.0
    return round(100.0 * part / whole, 1)
