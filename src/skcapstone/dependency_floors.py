"""Detect installed dependencies that do not satisfy this project's own declared floors.

``pyproject.toml`` states the versions skcapstone's code actually requires, but on
an editable install nothing re-checks that statement after the code changes. ``git
pull`` updates ``src/`` and ``pyproject.toml`` together and never re-resolves
dependencies, so a floor bump that lands with the code it exists to protect has no
effect on an environment that is already installed. The result is a host running
code whose stated dependency contract is unmet, with no signal until something deep
inside a module raises ``ImportError``.

This module makes that state answerable as a question instead of discoverable as a
crash. It reads the requirements skcapstone declares, compares them against what is
actually installed in the running interpreter, and reports the ones that do not
match.

Two deliberate choices:

* **The declared requirements are read from ``pyproject.toml`` when running from a
  source checkout**, and only from installed distribution metadata otherwise. On an
  editable install the metadata is a snapshot of the last ``pip install -e .`` and
  is exactly the stale artefact this module exists to catch; the checkout's
  ``pyproject.toml`` is what ``git pull`` just updated, and therefore what the code
  now on disk actually requires.

* **Comparison goes through ``packaging``**, never string equality. Dev builds
  (``0.1.78.dev1``) and local version segments (``+g<sha>``) are the normal state on
  this fleet, and pre-releases are accepted so that a dev build of a satisfying
  version is not reported as a violation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: Distribution whose declared requirements are checked.
DIST_NAME = "skcapstone"

#: Sentinel recorded as the installed version when a package is absent entirely.
NOT_INSTALLED = "not installed"


@dataclass(frozen=True)
class FloorViolation:
    """A declared requirement the running environment does not satisfy.

    Attributes:
        name: Distribution name of the dependency.
        installed: Installed version, or :data:`NOT_INSTALLED`.
        specifier: The declared version specifier, e.g. ``>=0.1.78``.
        extra: Optional-dependency group the requirement came from, or None
            when it is a core dependency.
    """

    name: str
    installed: str
    specifier: str
    extra: Optional[str] = None

    @property
    def missing(self) -> bool:
        """Whether the dependency is absent rather than merely the wrong version."""
        return self.installed == NOT_INSTALLED

    @property
    def fix(self) -> str:
        """Shell command that would satisfy this requirement."""
        return f"pip install -U '{self.name}{self.specifier}'"

    def __str__(self) -> str:
        where = f"[{self.extra}] " if self.extra else ""
        if self.missing:
            return f"{where}{self.name} not installed, {self.name}{self.specifier} required"
        return (
            f"{where}{self.name} {self.installed} installed, "
            f"{self.name}{self.specifier} required"
        )


def _project_root() -> Optional[Path]:
    """Locate the source checkout this package is running from.

    Returns:
        Directory containing a ``pyproject.toml`` that declares
        :data:`DIST_NAME`, or None when running from an installed wheel.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            return None
        # Cheap guard against picking up an unrelated pyproject.toml higher up
        # the tree; the full parse happens in _requirements_from_pyproject.
        if f'name = "{DIST_NAME}"' in text:
            return parent
        return None
    return None


def _requirements_from_pyproject(root: Path) -> Optional[list[tuple[str, Optional[str]]]]:
    """Read declared requirements out of a checkout's ``pyproject.toml``.

    Args:
        root: Directory containing ``pyproject.toml``.

    Returns:
        List of ``(requirement_string, extra_name_or_None)``, or None when the
        file cannot be parsed (no TOML reader, unreadable, malformed).
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python 3.10
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("dependency_floors: pyproject unreadable: %s", exc)
        return None

    project = data.get("project")
    if not isinstance(project, dict):
        return None

    out: list[tuple[str, Optional[str]]] = []
    for raw in project.get("dependencies") or []:
        if isinstance(raw, str):
            out.append((raw, None))
    extras = project.get("optional-dependencies")
    if isinstance(extras, dict):
        for extra, reqs in extras.items():
            for raw in reqs or []:
                if isinstance(raw, str):
                    out.append((raw, extra))
    return out


def _requirements_from_metadata() -> list[tuple[str, Optional[str]]]:
    """Read declared requirements out of installed distribution metadata.

    Returns:
        List of ``(requirement_string, extra_name_or_None)``. Empty when the
        distribution is not installed or declares nothing.
    """
    try:
        from importlib.metadata import requires
    except ImportError:  # pragma: no cover
        return []
    try:
        raw_reqs = requires(DIST_NAME)
    except PackageNotFoundError:
        return []
    if not raw_reqs:
        return []
    # Requires-Dist carries the extra in an environment marker rather than in a
    # separate table, so the marker evaluation in check_floors handles the split.
    return [(raw, None) for raw in raw_reqs]


def declared_requirements() -> list[tuple[str, Optional[str]]]:
    """Return the requirements skcapstone declares, freshest source first.

    Prefers the checkout's ``pyproject.toml`` (what the code on disk requires
    right now) and falls back to installed metadata (what the last install
    recorded).

    Returns:
        List of ``(requirement_string, extra_name_or_None)``.
    """
    root = _project_root()
    if root is not None:
        from_toml = _requirements_from_pyproject(root)
        if from_toml:
            return from_toml
    return _requirements_from_metadata()


def check_floors(
    requirements: Optional[list[tuple[str, Optional[str]]]] = None,
) -> list[FloorViolation]:
    """Report declared requirements the running environment does not satisfy.

    A core dependency is a violation when it is absent or when its installed
    version falls outside the declared specifier. An optional-dependency group
    is checked only where it is already installed: an absent extra is a choice,
    not a breach, but an installed one below its floor is the same hazard as a
    core dependency.

    Args:
        requirements: ``(requirement_string, extra_name_or_None)`` pairs to
            check. Defaults to :func:`declared_requirements`.

    Returns:
        Violations, deduplicated, ordered by dependency name.
    """
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version

    reqs = declared_requirements() if requirements is None else requirements
    seen: set[tuple[str, str, Optional[str]]] = set()
    violations: list[FloorViolation] = []

    for raw, extra in reqs:
        try:
            req = Requirement(raw)
        except InvalidRequirement as exc:
            logger.debug("dependency_floors: unparseable requirement %r: %s", raw, exc)
            continue
        if req.marker is not None and not req.marker.evaluate({"extra": extra or ""}):
            continue
        if not req.specifier:
            continue

        try:
            installed = version(req.name)
        except PackageNotFoundError:
            # An absent extra is not a breach; an absent core dependency is.
            if extra is None:
                key = (req.name, str(req.specifier), extra)
                if key not in seen:
                    seen.add(key)
                    violations.append(
                        FloorViolation(req.name, NOT_INSTALLED, str(req.specifier), extra)
                    )
            continue
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("dependency_floors: version lookup failed for %s: %s", req.name, exc)
            continue

        try:
            parsed = Version(installed)
        except InvalidVersion:
            # An unparseable installed version cannot be compared. Staying quiet
            # is the honest answer; claiming a violation would be a guess.
            logger.debug("dependency_floors: unparseable version %r for %s", installed, req.name)
            continue

        # prereleases=True: dev builds are the normal state on this fleet, and a
        # pre-release that satisfies the floor is not a violation.
        if req.specifier.contains(parsed, prereleases=True):
            continue

        key = (req.name, str(req.specifier), extra)
        if key in seen:
            continue
        seen.add(key)
        violations.append(FloorViolation(req.name, installed, str(req.specifier), extra))

    return sorted(violations, key=lambda v: (v.name, v.specifier))
