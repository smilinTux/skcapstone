"""Tests for skcapstone.fleet.textformat.humanize_bytes."""

from __future__ import annotations

from skcapstone.fleet.textformat import humanize_bytes


def test_humanize_bytes_zero() -> None:
    """0 bytes renders as '0B'."""
    assert humanize_bytes(0) == "0B"


def test_humanize_bytes_single_byte() -> None:
    """1 byte renders as '1B'."""
    assert humanize_bytes(1) == "1B"


def test_humanize_bytes_small_bytes() -> None:
    """Small byte counts render without decimal."""
    assert humanize_bytes(999) == "999B"


def test_humanize_bytes_1_5_kb() -> None:
    """1536 bytes renders as '1.5KB'."""
    assert humanize_bytes(1536) == "1.5KB"


def test_humanize_bytes_1_kb() -> None:
    """1024 bytes renders as '1.0KB'."""
    assert humanize_bytes(1024) == "1.0KB"


def test_humanize_bytes_5_kb() -> None:
    """5120 bytes renders as '5.0KB'."""
    assert humanize_bytes(5120) == "5.0KB"


def test_humanize_bytes_mb() -> None:
    """1 MB renders as '1.0MB'."""
    assert humanize_bytes(1048576) == "1.0MB"


def test_humanize_bytes_gb() -> None:
    """1073741824 bytes (1 GB) renders as '1.0GB'."""
    assert humanize_bytes(1073741824) == "1.0GB"


def test_humanize_bytes_fractional_gb() -> None:
    """1.5 GB renders as '1.5GB'."""
    assert humanize_bytes(int(1.5 * (1024**3))) == "1.5GB"


def test_humanize_bytes_tb() -> None:
    """1 TB renders as '1.0TB'."""
    assert humanize_bytes(1024**4) == "1.0TB"


def test_humanize_bytes_pb() -> None:
    """1 PB renders as '1.0PB'."""
    assert humanize_bytes(1024**5) == "1.0PB"


def test_humanize_bytes_larger_than_pb() -> None:
    """Values above PB still render correctly using PB unit."""
    assert humanize_bytes(2 * 1024**5) == "2.0PB"


def test_humanize_bytes_no_io() -> None:
    """Function is pure: no I/O, no side effects."""
    import inspect

    source = inspect.getsource(humanize_bytes)
    # Should not import os, sys, or open built-in
    assert "import os" not in source
    assert "import sys" not in source
    assert "open(" not in source
