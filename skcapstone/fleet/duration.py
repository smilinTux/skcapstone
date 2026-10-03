"""Pure duration/uptime formatting for fleet display."""

from __future__ import annotations


def format_uptime(seconds: int) -> str:
    """Render a non-negative duration as compact descending units.

    Units are d/h/m/s, omitting any leading zero units. Examples::

        0       -> '0s'
        45      -> '45s'
        90      -> '1m30s'
        3661    -> '1h1m1s'
        90061   -> '1d1h1m1s'

    Args:
        seconds: Duration in whole seconds (must be non-negative).

    Returns:
        Compact duration string with descending units.

    Raises:
        ValueError: If *seconds* is negative.
    """
    if seconds < 0:
        raise ValueError(f"uptime must be non-negative, got {seconds}")

    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")

    return "".join(parts)
