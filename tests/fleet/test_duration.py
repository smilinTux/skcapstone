"""Tests for skcapstone.fleet.duration.format_uptime."""

from __future__ import annotations

from skcapstone.fleet.duration import format_uptime


def test_format_uptime_zero() -> None:
    """0 seconds renders as '0s'."""
    assert format_uptime(0) == "0s"


def test_format_uptime_seconds_only() -> None:
    """Values under a minute render as seconds."""
    assert format_uptime(45) == "45s"


def test_format_uptime_exact_minute() -> None:
    """90 seconds renders as '1m30s'."""
    assert format_uptime(90) == "1m30s"


def test_format_uptime_hours() -> None:
    """3661 seconds renders as '1h1m1s'."""
    assert format_uptime(3661) == "1h1m1s"


def test_format_uptime_days() -> None:
    """90061 seconds renders as '1d1h1m1s'."""
    assert format_uptime(90061) == "1d1h1m1s"


def test_format_uptime_negative_raises() -> None:
    """Negative input raises ValueError."""
    try:
        format_uptime(-1)
    except ValueError:
        return
    raise AssertionError("expected ValueError for negative input")
