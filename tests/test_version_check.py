"""Tests for ecosystem package version checks."""

from __future__ import annotations

from skcapstone.version_check import _get_installed_version


def test_missing_package_returns_none_without_name_error():
    """Missing package lookup should not raise when logging the fallback failure."""
    assert _get_installed_version("definitely-not-an-sk-package") is None
