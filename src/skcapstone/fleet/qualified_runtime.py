"""Shared qualified-tool inventory and read-only target-prefix diagnostics."""

import sys
from pathlib import Path

TOOL_PACKAGES = ("pytest", "_pytest", "pytest_asyncio", "ruff", "pluggy", "iniconfig", "packaging")


def missing_dependencies(prefix: Path) -> list[str]:
    """Return missing fingerprint inputs in the node's qualified interpreter prefix."""
    site = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = site / "site-packages"
    missing = [name for name in TOOL_PACKAGES if not (site / name).is_dir()]
    if not (prefix / "bin/ruff").is_file():
        missing.append("ruff executable")
    return missing
