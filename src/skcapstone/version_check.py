"""
Ecosystem version checker for the sovereign agent stack.

Compares installed package versions against the latest available on PyPI.
Surfaces outdated packages in ``skcapstone doctor`` and provides a
standalone ``skcapstone version-check`` CLI command.
"""

from __future__ import annotations

import json
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


ECOSYSTEM_PACKAGES = [
    "skmemory",
    "skcapstone",
    "capauth",
    "sksecurity",
    "skcomm",
    "skchat",
    "cloud9-protocol",
]


@dataclass
class PackageVersion:
    """Version info for a single package.

    Attributes:
        name: Package name.
        installed: Installed version, or None if not installed.
        latest: Latest version on PyPI, or None if unavailable.
        up_to_date: Whether installed matches latest.
    """

    name: str
    installed: Optional[str] = None
    latest: Optional[str] = None
    up_to_date: bool = True


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
    except Exception:
        # Try import-based fallback for packages with dashes
        try:
            mod_name = package_name.replace("-", "_")
            import importlib

            mod = importlib.import_module(mod_name)
            return getattr(mod, "__version__", None)
        except Exception:
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

        up_to_date = True
        if installed and latest:
            up_to_date = installed == latest

        report.packages.append(PackageVersion(
            name=name,
            installed=installed,
            latest=latest,
            up_to_date=up_to_date,
        ))

    return report
