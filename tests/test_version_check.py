"""Tests for ecosystem package version checks."""

from __future__ import annotations

from skcapstone.version_check import (
    ECOSYSTEM_PACKAGES,
    VERSION_AHEAD,
    VERSION_CURRENT,
    VERSION_OUTDATED,
    VERSION_UNKNOWN,
    PackageVersion,
    VersionReport,
    _get_installed_version,
    compare_versions,
)


def test_missing_package_returns_none_without_name_error():
    """Missing package lookup should not raise when logging the fallback failure."""
    assert _get_installed_version("definitely-not-an-sk-package") is None


def test_ecosystem_uses_canonical_distribution_names():
    """Version checks must query distributions, not import names or retired shims."""
    assert "skcomms" in ECOSYSTEM_PACKAGES
    assert "skchat-sovereign" in ECOSYSTEM_PACKAGES
    assert "cloud9-protocol" in ECOSYSTEM_PACKAGES
    assert not {"skcomm", "skchat", "cloud9"} & set(ECOSYSTEM_PACKAGES)


# ───────────────────────────────────────────────────────────────────────────
# PEP 440 comparison
#
# An editable install from a checkout ahead of the last PyPI release is the
# normal state on this fleet, so a string-equality comparison reported every
# dev host as "outdated" and told the operator to downgrade. A check that is
# permanently and wrongly red trains everyone to ignore the whole report.
# ───────────────────────────────────────────────────────────────────────────


def test_dev_build_ahead_of_pypi_is_not_outdated():
    """0.15.168.dev222+gd448c2fa is NEWER than 0.15.166, not older."""
    assert compare_versions("0.15.168.dev222+gd448c2fa", "0.15.166") == VERSION_AHEAD


def test_local_segment_at_same_public_version_is_ahead():
    """PEP 440: a local segment sorts above the same public version."""
    assert compare_versions("0.14.266+gb606d822c", "0.14.266") == VERSION_AHEAD


def test_genuinely_behind_is_outdated():
    assert compare_versions("0.11.25", "0.11.26") == VERSION_OUTDATED


def test_equal_versions_are_current():
    assert compare_versions("0.2.18", "0.2.18") == VERSION_CURRENT


def test_padded_and_normalised_forms_compare_equal():
    """String equality also mis-reported equivalent PEP 440 spellings."""
    assert compare_versions("1.2.0", "1.2") == VERSION_CURRENT


def test_unparseable_version_is_unknown_never_outdated():
    """An unanswerable comparison must not masquerade as 'you are behind'."""
    assert compare_versions("not-a-version", "0.1.0") == VERSION_UNKNOWN
    assert compare_versions("0.1.0", None) == VERSION_UNKNOWN


def test_report_does_not_list_ahead_packages_as_outdated():
    """The report-level view the CLI and doctor consume must agree."""
    ahead = PackageVersion(
        name="skcapstone",
        installed="0.15.168.dev222+gd448c2fa",
        latest="0.15.166",
        status=VERSION_AHEAD,
    )
    behind = PackageVersion(
        name="skmemory", installed="0.11.25", latest="0.11.26", status=VERSION_OUTDATED
    )
    report = VersionReport(packages=[ahead, behind])
    assert ahead.up_to_date is True
    assert behind.up_to_date is False
    assert [p.name for p in report.outdated] == ["skmemory"]
    assert report.all_up_to_date is False


def test_check_versions_classifies_a_dev_install_as_ahead(monkeypatch):
    """End to end through check_versions, the path doctor actually calls."""
    import skcapstone.version_check as vc

    monkeypatch.setattr(vc, "_get_installed_version", lambda n: "0.15.168.dev222+gd448c2fa")
    monkeypatch.setattr(vc, "_get_pypi_version", lambda n, timeout=5.0: "0.15.166")
    report = vc.check_versions(packages=["skcapstone"], check_pypi=True)
    assert report.packages[0].status == VERSION_AHEAD
    assert report.outdated == []
    assert report.all_up_to_date is True
