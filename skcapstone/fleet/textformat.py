"""Pure text formatting utilities for fleet node display."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def humanize_bytes(n: int) -> str:
    """Render a non-negative byte count in binary (1024-based) units.

    Args:
        n: The number of bytes (must be non-negative).

    Returns:
        A human-readable string such as ``'1.5KB'``, ``'1.0GB'``,
        or ``'0B'`` for zero.

    Raises:
        ValueError: If *n* is negative.
    """
    if n < 0:
        raise ValueError(f"byte count must be non-negative, got {n}")

    if n == 0:
        return "0B"

    for i, unit in enumerate(_UNITS):
        if i == len(_UNITS) - 1 or n < 1024 ** (i + 1):
            if i == 0:
                return f"{n}B"
            return f"{n / (1024 ** i):.1f}{unit}"
