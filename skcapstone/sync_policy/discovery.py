"""Syncthing config discovery for the private-material policy audit.

Finds every configured Syncthing folder by parsing config.xml from the
standard locations (or an explicit override), expands ``~``, resolves
symlinks, and deduplicates by resolved root. Discovery failures are
error-grade findings: a config that cannot be found or parsed cannot be
vouched for, so the audit fails closed.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .model import Finding

#: Syncthing config locations relative to a home directory, most common first.
CONFIG_CANDIDATES = (
    Path(".config/syncthing/config.xml"),
    Path(".local/state/syncthing/config.xml"),
)


@dataclass(frozen=True)
class DiscoveredFolder:
    """One Syncthing folder entry with its resolved root path."""

    folder_id: str
    path: Path


def default_config_paths(home: Path) -> list[Path]:
    """The standard Syncthing config.xml locations under one home.

    Args:
        home: Home directory to resolve against.

    Returns:
        Candidate config paths in preference order.
    """
    return [home / candidate for candidate in CONFIG_CANDIDATES]


def parse_config_folders(config_path: Path) -> tuple[list[DiscoveredFolder], Finding | None]:
    """Extract folder ids and resolved roots from one Syncthing config.

    Args:
        config_path: Path to a config.xml.

    Returns:
        Discovered folders with ``~`` expanded and symlinks resolved, plus
        an error finding when the file cannot be read or parsed.
    """
    try:
        tree = ET.parse(config_path)
    except (ET.ParseError, OSError) as exc:
        return [], Finding(
            severity="error",
            category="config_unreadable",
            path=str(config_path),
            detail=f"Syncthing config cannot be parsed: {exc}",
        )
    folders: list[DiscoveredFolder] = []
    for elem in tree.getroot().iter("folder"):
        folder_id = (elem.get("id") or "").strip()
        raw_path = (elem.get("path") or "").strip()
        if not folder_id or not raw_path:
            continue
        expanded = Path(raw_path).expanduser()
        try:
            resolved = expanded.resolve()
        except OSError:
            resolved = expanded.absolute()
        folders.append(DiscoveredFolder(folder_id=folder_id, path=resolved))
    return folders, None


def discover_folders(
    home: Path,
    config_path: Path | None = None,
) -> tuple[list[DiscoveredFolder], list[Finding]]:
    """Discover every configured folder across all known config locations.

    Args:
        home: Home directory for standard config locations.
        config_path: Explicit config.xml override; a missing explicit path
            is an error-grade finding.

    Returns:
        Folders deduplicated by resolved root, plus discovery findings.
        Finding no config at all is error-grade: the audit cannot prove no
        folder exists, so it fails closed.
    """
    if config_path is not None:
        if not config_path.is_file():
            return [], [
                Finding(
                    severity="error",
                    category="config_not_found",
                    path=str(config_path),
                    detail="explicit Syncthing config path does not exist",
                )
            ]
        configs = [config_path]
    else:
        configs = [path for path in default_config_paths(home) if path.is_file()]
        if not configs:
            return [], [
                Finding(
                    severity="error",
                    category="config_not_found",
                    detail="no Syncthing config.xml found in any standard location",
                )
            ]

    findings: list[Finding] = []
    folders: list[DiscoveredFolder] = []
    for config in configs:
        parsed, problem = parse_config_folders(config)
        if problem is not None:
            findings.append(problem)
        folders.extend(parsed)

    deduped: dict[Path, DiscoveredFolder] = {}
    for folder in folders:
        deduped.setdefault(folder.path, folder)
    return list(deduped.values()), findings
