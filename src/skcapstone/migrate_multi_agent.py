"""
Migrate from single-agent to multi-agent household layout.

Moves per-agent data (identity, memory, soul, trust, security, config)
from the flat ~/.skcapstone/ root into ~/.skcapstone/agents/{name}/,
while shared data (coordination, heartbeats, peers, sync, pubsub,
file-transfer) stays at the root.

Usage:
    skcapstone migrate --agent opus
    skcapstone migrate --agent opus --dry-run
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.migrate")

# Directories/files that are per-agent (move into agents/{name}/)
PER_AGENT_DIRS = [
    "identity",
    "memory",
    "soul",
    "trust",
    "security",
    "config",
    "comms",
    "logs",
]

PER_AGENT_FILES = [
    "manifest.json",
    "skcomm.yml",
]

# Directories that stay at root (shared infrastructure)
SHARED_DIRS = [
    "coordination",
    "heartbeats",
    "peers",
    "sync",
    "pubsub",
    "file-transfer",
    "backups",
]


def migrate_to_multi_agent(
    root: Path,
    agent_name: str,
    dry_run: bool = False,
) -> dict:
    """Migrate existing single-agent layout to multi-agent household.

    Args:
        root: The skcapstone root directory (~/.skcapstone).
        agent_name: Name for the agent (e.g., 'opus').
        dry_run: If True, report what would happen without moving anything.

    Returns:
        Dict with migration results (moved, skipped, errors).
    """
    root = Path(root).expanduser()
    agent_home = root / "agents" / agent_name
    results = {
        "agent_name": agent_name,
        "agent_home": str(agent_home),
        "dry_run": dry_run,
        "moved": [],
        "skipped": [],
        "errors": [],
        "symlinks_created": [],
    }

    if agent_home.exists():
        logger.info("Agent home already exists: %s", agent_home)
        results["skipped"].append(f"agents/{agent_name}/ already exists")
        return results

    if not dry_run:
        agent_home.mkdir(parents=True, exist_ok=True)

    # Move per-agent directories
    for dirname in PER_AGENT_DIRS:
        src = root / dirname
        dst = agent_home / dirname
        if src.exists() and not src.is_symlink():
            if dry_run:
                results["moved"].append(f"{dirname}/ -> agents/{agent_name}/{dirname}/")
            else:
                try:
                    shutil.move(str(src), str(dst))
                    # Create symlink for backward compat
                    src.symlink_to(dst)
                    results["moved"].append(f"{dirname}/")
                    results["symlinks_created"].append(f"{dirname} -> agents/{agent_name}/{dirname}")
                    logger.info("Moved %s -> %s (symlinked)", src, dst)
                except Exception as exc:
                    results["errors"].append(f"{dirname}: {exc}")
                    logger.error("Failed to move %s: %s", dirname, exc)
        else:
            if src.is_symlink():
                results["skipped"].append(f"{dirname}/ (already a symlink)")
            else:
                results["skipped"].append(f"{dirname}/ (not found)")

    # Move per-agent files
    for filename in PER_AGENT_FILES:
        src = root / filename
        dst = agent_home / filename
        if src.exists() and not src.is_symlink():
            if dry_run:
                results["moved"].append(f"{filename} -> agents/{agent_name}/{filename}")
            else:
                try:
                    shutil.move(str(src), str(dst))
                    src.symlink_to(dst)
                    results["moved"].append(filename)
                    results["symlinks_created"].append(f"{filename} -> agents/{agent_name}/{filename}")
                    logger.info("Moved %s -> %s (symlinked)", src, dst)
                except Exception as exc:
                    results["errors"].append(f"{filename}: {exc}")
        else:
            results["skipped"].append(f"{filename} (not found or symlink)")

    # Write migration marker
    if not dry_run:
        marker = {
            "migrated_at": datetime.now(timezone.utc).isoformat(),
            "agent_name": agent_name,
            "layout": "multi-agent",
            "version": "1.0",
        }
        marker_path = agent_home / ".migration.json"
        marker_path.write_text(json.dumps(marker, indent=2), encoding="utf-8")

    return results


def create_agent_home(
    root: Path,
    agent_name: str,
    dry_run: bool = False,
) -> dict:
    """Create a fresh agent home directory with empty structure.

    Args:
        root: The skcapstone root directory (~/.skcapstone).
        agent_name: Name for the new agent (e.g., 'jarvis').
        dry_run: If True, report what would be created.

    Returns:
        Dict with creation results.
    """
    root = Path(root).expanduser()
    agent_home = root / "agents" / agent_name
    results = {
        "agent_name": agent_name,
        "agent_home": str(agent_home),
        "dry_run": dry_run,
        "created": [],
        "skipped": [],
    }

    if agent_home.exists():
        results["skipped"].append("Agent home already exists")
        return results

    if dry_run:
        results["created"] = [
            f"agents/{agent_name}/",
            f"agents/{agent_name}/identity/",
            f"agents/{agent_name}/memory/short-term/",
            f"agents/{agent_name}/memory/mid-term/",
            f"agents/{agent_name}/memory/long-term/",
            f"agents/{agent_name}/soul/",
            f"agents/{agent_name}/trust/",
            f"agents/{agent_name}/security/",
            f"agents/{agent_name}/config/",
        ]
        return results

    # Create directory structure
    dirs = [
        agent_home / "identity",
        agent_home / "memory" / "short-term",
        agent_home / "memory" / "mid-term",
        agent_home / "memory" / "long-term",
        agent_home / "soul",
        agent_home / "trust",
        agent_home / "security",
        agent_home / "config",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        results["created"].append(str(d.relative_to(root)))

    # Write minimal manifest
    manifest = {
        "name": agent_name,
        "version": "0.1.0",
        "entity_type": "ai-agent",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path = agent_home / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    results["created"].append(str(manifest_path.relative_to(root)))

    return results


def is_multi_agent(root: Path) -> bool:
    """Check whether the root is already in multi-agent layout.

    Args:
        root: The skcapstone root directory.

    Returns:
        True if agents/ directory exists with at least one agent.
    """
    agents_dir = Path(root).expanduser() / "agents"
    if not agents_dir.exists():
        return False
    return any(d.is_dir() for d in agents_dir.iterdir())


def list_agents(root: Path) -> list[str]:
    """List all agent names in the household.

    Args:
        root: The skcapstone root directory.

    Returns:
        Sorted list of agent directory names.
    """
    agents_dir = Path(root).expanduser() / "agents"
    if not agents_dir.exists():
        return []
    return sorted(
        d.name for d in agents_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )
