"""Shared qualified-tool inventory and read-only target-prefix diagnostics."""

import sys
from pathlib import Path

TOOL_PACKAGES = ("pytest", "_pytest", "pytest_asyncio", "ruff", "pluggy", "iniconfig", "packaging")

REQUIRED_PACKAGES = (*TOOL_PACKAGES, "ansible", "skcapstone", "skharness")

#: The clean qualification prefix, relative to the operator home. It lives
#: outside the Syncthing-shared estate tree on purpose: every host builds its
#: own copy from the same pinned lock, and production ~/.skenv is never used
#: for sealed test execution.
QUALIFY_PREFIX = Path(".local/share/skcapstone/qualify-env")

#: Deterministic build record written inside the prefix and bound into the
#: runtime and toolchain fingerprints.
STATE_NAME = "qualify-manifest.json"


def qualify_prefix(home: Path) -> Path:
    """Return the clean sealed-qualification prefix for one operator home."""
    return Path(home) / QUALIFY_PREFIX


def missing_dependencies(prefix: Path) -> list[str]:
    """Return missing prefix-contained dependencies for governed qualification."""
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = site / "site-packages"
    missing = [
        name
        for name in REQUIRED_PACKAGES
        if not (site / name).is_dir()
        or not (site / name).resolve().is_relative_to(prefix.resolve())
    ]
    core_init = site / "skcapstone/__init__.py"
    if "skcapstone" not in missing and (
        not core_init.is_file() or not core_init.resolve().is_relative_to(prefix.resolve())
    ):
        missing.append("skcapstone")
    if not (prefix / "bin/ruff").is_file():
        missing.append("ruff executable")
    return missing
