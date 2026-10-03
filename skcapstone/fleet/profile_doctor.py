"""Pure drift diff: observed node inventory against its install profile.

Epic 3bbf39ea, card ffcb1a3d (parent cd5ef08b). One function, one frozen
result, no side effects at all. The inventory comes from nodeinventory.py
(observe), the profile from profiles.normalize_profile_spec (declare), and
this module is the subtraction between them.

REPORT ONLY, and structurally so. There is no actuation verb here and no way
to reach one: this module cannot start, stop, enable, disable or install
anything, because it does not touch the host. A bug here produces a wrong
FINDING, never a wrong CHANGE. That separation is the whole reason the drift
report can be trusted enough to run everywhere.

Severity grading is deliberately asymmetric:

    forbidden        error  something present that the profile forbids
    missing_required warn   something absent that the profile requires
    unexpected       info   something present the profile never mentions

`forbidden` is the only error grade because it is the only category that
means a node is doing something it was explicitly told not to do. A missing
required unit is usually a node mid-install. An unexpected unit is usually a
manifest that has not caught up with reality, and treating that as an error
would train everyone to ignore the report.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

#: Findings that mean "this node is doing something it was told not to".
ERROR_CATEGORIES = ("forbidden_units", "forbidden_packages")
#: Findings that mean "this node has not finished becoming what it should be".
WARN_CATEGORIES = ("missing_required_units", "missing_required_packages")
#: Findings that mean "the manifest and reality disagree, probably the manifest".
INFO_CATEGORIES = ("unexpected_units", "unexpected_packages")


@dataclass(frozen=True)
class DriftReport:
    """The six-way difference between what a node has and what it should have.

    Every field is a sorted list of names, so two reports over unchanged
    inputs compare equal and can be diffed across runs.
    """

    missing_required_units: list[str] = field(default_factory=list)
    forbidden_units: list[str] = field(default_factory=list)
    unexpected_units: list[str] = field(default_factory=list)
    missing_required_packages: list[str] = field(default_factory=list)
    forbidden_packages: list[str] = field(default_factory=list)
    unexpected_packages: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        """`error`, `warn`, `info` or `ok`, worst finding wins."""
        if any(getattr(self, name) for name in ERROR_CATEGORIES):
            return "error"
        if any(getattr(self, name) for name in WARN_CATEGORIES):
            return "warn"
        if any(getattr(self, name) for name in INFO_CATEGORIES):
            return "info"
        return "ok"

    @property
    def clean(self) -> bool:
        """True when every category is empty."""
        return self.severity == "ok"

    def as_dict(self) -> dict:
        """Machine-readable form, severity included."""
        return {
            "missing_required_units": list(self.missing_required_units),
            "forbidden_units": list(self.forbidden_units),
            "unexpected_units": list(self.unexpected_units),
            "missing_required_packages": list(self.missing_required_packages),
            "forbidden_packages": list(self.forbidden_packages),
            "unexpected_packages": list(self.unexpected_packages),
            "severity": self.severity,
        }

    def findings(self) -> list[tuple[str, str, str]]:
        """Flat (grade, category, name) rows, sorted, for table rendering."""
        rows = []
        for grade, names in (
            ("error", ERROR_CATEGORIES),
            ("warn", WARN_CATEGORIES),
            ("info", INFO_CATEGORIES),
        ):
            for category in names:
                for item in getattr(self, category):
                    rows.append((grade, category, item))
        return rows


def _ignored(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _diff_one(observed: list[str], rules: dict, ignore: list[str]) -> tuple[list, list, list]:
    """(missing_required, forbidden, unexpected) for one name space.

    The ignore patterns suppress `unexpected` only. A name the profile
    explicitly forbids stays forbidden even if an ignore glob would have
    covered it, because "I take no position on this" must never override
    "this must not be here".
    """
    present = set(observed)
    required = set(rules.get("required", []))
    allowed = set(rules.get("allowed", []))
    must_not = set(rules.get("mustNot", []))

    missing_required = sorted(required - present)
    forbidden = sorted(present & must_not)
    unexpected = sorted(
        name for name in present - allowed - must_not if not _ignored(name, ignore)
    )
    return missing_required, forbidden, unexpected


def diff(inventory: dict, profile: dict) -> DriftReport:
    """Compare one node's observed inventory against a normalized profile.

    Args:
        inventory: From nodeinventory.collect(). Units are read from
            ``units.user``; the system scope is distro baseline and is not
            what a role profile governs.
        profile: From profiles.normalize_profile_spec().

    Returns:
        A DriftReport. An empty profile (all name lists empty) yields only
        `unexpected` findings, never a wall of `forbidden`, which is what
        makes an unfinished manifest safe to ship.
    """
    units_present = sorted((inventory.get("units") or {}).get("user") or {})
    packages_present = sorted(inventory.get("packages") or {})
    ignore = list(profile.get("unitsIgnore") or [])

    missing_units, forbidden_units, unexpected_units = _diff_one(
        units_present, profile.get("units") or {}, ignore
    )
    # unitsIgnore is deliberately units-only: a package named like a desktop
    # unit is not the same class of noise, and silently reusing the patterns
    # would hide real package drift.
    missing_pkgs, forbidden_pkgs, unexpected_pkgs = _diff_one(
        packages_present, profile.get("packages") or {}, []
    )

    return DriftReport(
        missing_required_units=missing_units,
        forbidden_units=forbidden_units,
        unexpected_units=unexpected_units,
        missing_required_packages=missing_pkgs,
        forbidden_packages=forbidden_pkgs,
        unexpected_packages=unexpected_pkgs,
    )
