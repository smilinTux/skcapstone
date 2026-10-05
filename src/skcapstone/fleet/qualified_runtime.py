"""Shared qualified-tool inventory and read-only target-prefix diagnostics."""

import sys
from pathlib import Path

TOOL_PACKAGES = ("pytest", "_pytest", "pytest_asyncio", "ruff", "pluggy", "iniconfig", "packaging")

REQUIRED_PACKAGES = (*TOOL_PACKAGES, "ansible", "skcapstone")


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
