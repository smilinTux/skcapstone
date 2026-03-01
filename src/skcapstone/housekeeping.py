"""
Housekeeping — storage pruning for the sovereign agent.

Prunes stale files that accumulate in the agent profile:
- ACK files in ~/.skcomm/acks/ (age-based, 24h default)
- Delivered envelopes in ~/.skcapstone/sync/comms/outbox/ (age-based, 48h)
- Seed snapshots in ~/.skcapstone/sync/sync/outbox/ (count-based, keep 10)

These directories grow unbounded and can bloat a ~15MB profile to 300MB+.
Run via daemon loop (hourly) or CLI: ``skcapstone housekeeping [--dry-run]``.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.housekeeping")

DEFAULT_ACK_MAX_AGE_HOURS = 24
DEFAULT_COMMS_MAX_AGE_HOURS = 48
DEFAULT_SEEDS_KEEP_PER_AGENT = 10


def prune_acks(skcomm_home: Path, max_age_hours: int = DEFAULT_ACK_MAX_AGE_HOURS) -> int:
    """Remove ACK files older than max_age_hours.

    ACK files in ~/.skcomm/acks/ confirm message delivery but are never
    read after initial processing. They accumulate indefinitely.

    Args:
        skcomm_home: Path to ~/.skcomm.
        max_age_hours: Delete ACKs older than this. Default 24.

    Returns:
        Number of files deleted.
    """
    acks_dir = skcomm_home / "acks"
    if not acks_dir.is_dir():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for path in acks_dir.iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("Failed to delete ACK %s: %s", path.name, exc)

    if deleted:
        logger.info("Pruned %d ACK files from %s", deleted, acks_dir)
    return deleted


def prune_comms_outbox(
    sync_dir: Path, max_age_hours: int = DEFAULT_COMMS_MAX_AGE_HOURS
) -> int:
    """Remove delivered envelopes older than max_age_hours.

    The comms outbox at ~/.skcapstone/sync/comms/outbox/<agent>/
    stores serialized envelopes for Syncthing delivery. Once synced,
    they linger indefinitely.

    Args:
        sync_dir: Path to ~/.skcapstone/sync.
        max_age_hours: Delete envelopes older than this. Default 48.

    Returns:
        Number of files deleted.
    """
    outbox_dir = sync_dir / "comms" / "outbox"
    if not outbox_dir.is_dir():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for agent_dir in outbox_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        for path in agent_dir.iterdir():
            if not path.is_file():
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete envelope %s: %s", path.name, exc)

        # Remove empty agent directories
        try:
            if agent_dir.is_dir() and not any(agent_dir.iterdir()):
                agent_dir.rmdir()
        except OSError:
            pass

    if deleted:
        logger.info("Pruned %d comms outbox files from %s", deleted, outbox_dir)
    return deleted


def prune_seeds(
    outbox_dir: Path, keep_per_agent: int = DEFAULT_SEEDS_KEEP_PER_AGENT
) -> int:
    """Keep only the most recent seeds per agent, delete the rest.

    Seed files in ~/.skcapstone/sync/sync/outbox/ are named like
    ``<agent>-<timestamp>.json.gpg`` or ``<agent>-<timestamp>.json``.
    A new seed is pushed every 5 minutes by the daemon, so they
    accumulate quickly.

    Args:
        outbox_dir: Path to ~/.skcapstone/sync/sync/outbox.
        keep_per_agent: Number of most recent seeds to keep. Default 10.

    Returns:
        Number of files deleted.
    """
    if not outbox_dir.is_dir():
        return 0

    # Group seed files by agent prefix
    agent_files: dict[str, list[Path]] = defaultdict(list)

    for path in outbox_dir.iterdir():
        if not path.is_file():
            continue
        # Extract agent name: everything before the last dash-timestamp
        name = path.name
        # Patterns: agent-1709123456.json, agent-1709123456.json.gpg
        parts = name.rsplit("-", 1)
        if len(parts) == 2:
            agent_name = parts[0]
        else:
            agent_name = "__unknown__"
        agent_files[agent_name].append(path)

    deleted = 0
    for agent, files in agent_files.items():
        # Sort by mtime descending (newest first)
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old_file in files[keep_per_agent:]:
            try:
                old_file.unlink()
                deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete seed %s: %s", old_file.name, exc)

    if deleted:
        logger.info("Pruned %d seed files from %s", deleted, outbox_dir)
    return deleted


def run_housekeeping(
    skcapstone_home: Optional[Path] = None,
    skcomm_home: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """Run all housekeeping tasks.

    Args:
        skcapstone_home: Path to ~/.skcapstone. Defaults to AGENT_HOME.
        skcomm_home: Path to ~/.skcomm. Defaults to ~/.skcomm.
        dry_run: If True, report what would be deleted without deleting.

    Returns:
        Dict with counts per target and total bytes freed.
    """
    from . import AGENT_HOME

    if skcapstone_home is None:
        skcapstone_home = Path(AGENT_HOME).expanduser()
    if skcomm_home is None:
        skcomm_home = Path("~/.skcomm").expanduser()

    results: dict[str, dict] = {}

    # Measure sizes before pruning
    targets = {
        "acks": skcomm_home / "acks",
        "comms_outbox": skcapstone_home / "sync" / "comms" / "outbox",
        "seed_outbox": skcapstone_home / "sync" / "sync" / "outbox",
    }

    for key, path in targets.items():
        results[key] = {
            "path": str(path),
            "exists": path.is_dir(),
            "size_before": _dir_size(path),
        }

    if dry_run:
        # Count what would be deleted without deleting
        results["acks"]["would_delete"] = _count_stale_files(
            targets["acks"], DEFAULT_ACK_MAX_AGE_HOURS
        )
        results["comms_outbox"]["would_delete"] = _count_stale_comms(
            targets["comms_outbox"], DEFAULT_COMMS_MAX_AGE_HOURS
        )
        results["seed_outbox"]["would_delete"] = _count_excess_seeds(
            targets["seed_outbox"], DEFAULT_SEEDS_KEEP_PER_AGENT
        )
        results["dry_run"] = True
        return results

    # Actually prune
    results["acks"]["deleted"] = prune_acks(skcomm_home)
    results["comms_outbox"]["deleted"] = prune_comms_outbox(skcapstone_home / "sync")
    results["seed_outbox"]["deleted"] = prune_seeds(targets["seed_outbox"])

    # Measure sizes after
    for key, path in targets.items():
        results[key]["size_after"] = _dir_size(path)
        before = results[key]["size_before"]
        after = results[key]["size_after"]
        results[key]["freed"] = max(0, before - after)

    total_freed = sum(r.get("freed", 0) for r in results.values() if isinstance(r, dict))
    total_deleted = sum(r.get("deleted", 0) for r in results.values() if isinstance(r, dict))
    results["summary"] = {
        "total_deleted": total_deleted,
        "total_freed_bytes": total_freed,
        "total_freed_mb": round(total_freed / (1024 * 1024), 1),
    }

    return results


def _dir_size(path: Path) -> int:
    """Calculate total size of all files in a directory tree."""
    if not path.is_dir():
        return 0
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _count_stale_files(directory: Path, max_age_hours: int) -> int:
    """Count files older than max_age_hours (for dry-run)."""
    if not directory.is_dir():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    return sum(
        1
        for p in directory.iterdir()
        if p.is_file() and p.stat().st_mtime < cutoff
    )


def _count_stale_comms(outbox_dir: Path, max_age_hours: int) -> int:
    """Count stale comms outbox files (for dry-run)."""
    if not outbox_dir.is_dir():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0
    for agent_dir in outbox_dir.iterdir():
        if not agent_dir.is_dir():
            continue
        for p in agent_dir.iterdir():
            if p.is_file() and p.stat().st_mtime < cutoff:
                count += 1
    return count


def _count_excess_seeds(outbox_dir: Path, keep_per_agent: int) -> int:
    """Count excess seed files beyond keep_per_agent (for dry-run)."""
    if not outbox_dir.is_dir():
        return 0

    agent_files: dict[str, list[Path]] = defaultdict(list)
    for path in outbox_dir.iterdir():
        if not path.is_file():
            continue
        parts = path.name.rsplit("-", 1)
        agent_name = parts[0] if len(parts) == 2 else "__unknown__"
        agent_files[agent_name].append(path)

    count = 0
    for files in agent_files.values():
        excess = len(files) - keep_per_agent
        if excess > 0:
            count += excess
    return count
