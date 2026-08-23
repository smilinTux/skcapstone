"""Tests for ecosystem package version checks."""

from __future__ import annotations

from skcapstone.version_check import ECOSYSTEM_PACKAGES, _get_installed_version


def test_missing_package_returns_none_without_name_error():
    """Missing package lookup should not raise when logging the fallback failure."""
    assert _get_installed_version("definitely-not-an-sk-package") is None


def test_ecosystem_uses_canonical_distribution_names():
    """Version checks must query distributions, not import names or retired shims."""
    assert "skcomms" in ECOSYSTEM_PACKAGES
    assert "skchat-sovereign" in ECOSYSTEM_PACKAGES
    assert "cloud9-protocol" in ECOSYSTEM_PACKAGES
    assert not {"skcomm", "skchat", "cloud9"} & set(ECOSYSTEM_PACKAGES)
