"""
Ecosystem version checker for the sovereign agent stack.

Compares installed package versions against the latest available on PyPI.
Surfaces outdated packages in ``skcapstone doctor`` and provides a
standalone ``skcapstone version-check`` CLI command.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

# Comparison outcomes. Deliberately four states, not a boolean.
#
# Editable installs built from a checkout that is ahead of the last PyPI
# release are the NORMAL state on this fleet, and the previous string-equality
# test ("installed == latest") classified every one of them as outdated, with
# a suggested fix of `pip install --upgrade` that would in fact DOWNGRADE the
# host. A check that is permanently and wrongly red trains operators to ignore
# the whole report, so AHEAD is reported as informational rather than as a
# failure, and a comparison we cannot make is reported as UNKNOWN rather than
# defaulting to "you are behind".
VERSION_CURRENT = "current"
VERSION_OUTDATED = "outdated"
VERSION_AHEAD = "ahead"
VERSION_UNKNOWN = "unknown"


def compare_versions(installed: Optional[str], latest: Optional[str]) -> str:
    """Compare two versions under PEP 440.

    Handles development releases (``.devN``) and local version segments
    (``+g<sha>``), which setuptools-scm produces for any install built from a
    git checkout.

    Args:
        installed: Installed version string, or None.
        latest: Latest published version string, or None.

    Returns:
        One of VERSION_CURRENT, VERSION_OUTDATED, VERSION_AHEAD,
        VERSION_UNKNOWN.
    """
    if not installed or not latest:
        return VERSION_UNKNOWN
    try:
        have = Version(installed)
        want = Version(latest)
    except InvalidVersion as exc:
        logger.debug("version_check.py could not parse a version: %s", exc)
        return VERSION_UNKNOWN
    if have < want:
        return VERSION_OUTDATED
    if have > want:
        return VERSION_AHEAD
    return VERSION_CURRENT


ECOSYSTEM_PACKAGES = [
    "skmemory",
    "skcapstone",
    "capauth",
    "sksecurity",
    "skcomms",
    "skchat-sovereign",
    "cloud9-protocol",
]


@dataclass
class PackageVersion:
    """Version info for a single package.

    Attributes:
        name: Package name.
        installed: Installed version, or None if not installed.
        latest: Latest version on PyPI, or None if unavailable.
        status: PEP 440 comparison outcome (see VERSION_* constants).
    """

    name: str
    installed: Optional[str] = None
    latest: Optional[str] = None
    status: str = VERSION_UNKNOWN

    @property
    def up_to_date(self) -> bool:
        """Whether there is nothing to upgrade.

        True for CURRENT, AHEAD and UNKNOWN: only a package we can prove is
        behind its published release warrants an upgrade prompt.
        """
        return self.status != VERSION_OUTDATED


@dataclass
class VersionReport:
    """Aggregated version report for the ecosystem.

    Attributes:
        packages: List of per-package version info.
    """

    packages: list[PackageVersion] = field(default_factory=list)

    @property
    def all_up_to_date(self) -> bool:
        """Whether every installed package is up to date."""
        return all(p.up_to_date for p in self.packages if p.installed)

    @property
    def outdated(self) -> list[PackageVersion]:
        """Packages that are installed but not at the latest version."""
        return [p for p in self.packages if p.installed and not p.up_to_date]

    @property
    def missing(self) -> list[PackageVersion]:
        """Packages that are not installed at all."""
        return [p for p in self.packages if not p.installed]


def _get_installed_version(package_name: str) -> Optional[str]:
    """Get the installed version of a package.

    Args:
        package_name: Python package name.

    Returns:
        Version string or None.
    """
    try:
        from importlib.metadata import version

        return version(package_name)
    except Exception as e:
        logger.debug("version_check.py metadata lookup failed: %s", e)
        # Try import-based fallback for packages with dashes
        try:
            mod_name = package_name.replace("-", "_")
            import importlib

            mod = importlib.import_module(mod_name)
            return getattr(mod, "__version__", None)
        except Exception as e:
            logger.debug("version_check.py import fallback failed: %s", e)
            return None


def _get_pypi_version(package_name: str, timeout: float = 5.0) -> Optional[str]:
    """Query PyPI JSON API for the latest version.

    Args:
        package_name: Package name on PyPI.
        timeout: HTTP timeout in seconds.

    Returns:
        Latest version string, or None if unavailable.
    """
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            return data.get("info", {}).get("version")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        return None


def check_versions(
    packages: Optional[list[str]] = None,
    check_pypi: bool = True,
) -> VersionReport:
    """Check installed vs latest versions for ecosystem packages.

    Args:
        packages: Package names to check (default: ECOSYSTEM_PACKAGES).
        check_pypi: Whether to query PyPI for latest versions.

    Returns:
        VersionReport with per-package results.
    """
    pkg_list = packages or ECOSYSTEM_PACKAGES
    report = VersionReport()

    for name in pkg_list:
        installed = _get_installed_version(name)
        latest = _get_pypi_version(name) if check_pypi else None

        report.packages.append(
            PackageVersion(
                name=name,
                installed=installed,
                latest=latest,
                status=compare_versions(installed, latest),
            )
        )

    return report
