"""
Housekeeping — storage pruning for the sovereign agent.

Prunes stale files that accumulate in the agent profile:
- ACK files in ~/.skcomms/acks/ (age-based, 24h default)
- Delivered envelopes in ~/.skcapstone/sync/comms/outbox/ (age-based, 48h)
- Seed snapshots in ~/.skcapstone/sync/outbox/ (count-based, keep 10)
- Legacy v1 comms outboxes (age-based, 7d) — both the root path
  ~/.skcapstone/comms/outbox/<recipient>/ and every per-agent path
  ~/.skcapstone/agents/<agent>/comms/outbox/<recipient>/, plus any v1
  broadcast subdir literally named ``*`` (removed wholesale)

These directories grow unbounded and can bloat a ~15MB profile to 300MB+.
Run via daemon loop (hourly) or CLI: ``skcapstone housekeeping [--dry-run]``.

Incident background (2026-06-16): a Framework 13 laptop overheated because
~/.skcapstone had grown to 462k files. Root cause was ~256k stale v1 broadcast
envelopes accumulating in directories literally named ``*`` (a v1
``recipient="*"`` presence-broadcast was written as a literal ``*`` directory).
They lived in the legacy v1 outbox paths that the v2-only housekeeping never
swept, so they grew unbounded. :func:`prune_legacy_comms` sweeps those paths.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.housekeeping")

DEFAULT_ACK_MAX_AGE_HOURS = 24
DEFAULT_COMMS_MAX_AGE_HOURS = 48
DEFAULT_SEEDS_KEEP_PER_AGENT = 10
DEFAULT_LEGACY_COMMS_MAX_AGE_HOURS = 168  # 7 days — legacy v1 data is long-dead
# Inbox TTL is a BACKSTOP only: delete-on-consume in the consciousness loop
# (consciousness_loop._consume_inbox_file) is the primary GC guarantee. This TTL
# reclaims envelopes on nodes with no live consumer. It must comfortably exceed
# consume latency — never set it below the daemon poll interval.
DEFAULT_INBOX_MAX_AGE_HOURS = 168  # 7 days


def prune_acks(skcomms_home: Path, max_age_hours: int = DEFAULT_ACK_MAX_AGE_HOURS) -> int:
    """Remove ACK files older than max_age_hours.

    ACK files in ~/.skcomms/acks/ confirm message delivery but are never
    read after initial processing. They accumulate indefinitely.

    Args:
        skcomms_home: Path to ~/.skcomms.
        max_age_hours: Delete ACKs older than this. Default 24.

    Returns:
        Number of files deleted.
    """
    acks_dir = skcomms_home / "acks"
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


def prune_comms_outbox(sync_dir: Path, max_age_hours: int = DEFAULT_COMMS_MAX_AGE_HOURS) -> int:
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


def prune_seeds(outbox_dir: Path, keep_per_agent: int = DEFAULT_SEEDS_KEEP_PER_AGENT) -> int:
    """Keep only the most recent seeds per agent, delete the rest.

    Seed files in ~/.skcapstone/sync/outbox/ are named like
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


def _legacy_outbox_dirs(skcapstone_home: Path) -> list[Path]:
    """Return all legacy v1 comms-outbox roots that exist under *skcapstone_home*.

    Two legacy layouts are swept:
    - ``<home>/comms/outbox`` — the v1 root path.
    - ``<home>/agents/<agent>/comms/outbox`` — the v1 per-agent path.

    Args:
        skcapstone_home: Path to ~/.skcapstone.

    Returns:
        List of existing outbox directories (may be empty).
    """
    roots: list[Path] = []

    root_outbox = skcapstone_home / "comms" / "outbox"
    if root_outbox.is_dir():
        roots.append(root_outbox)

    agents_dir = skcapstone_home / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            agent_outbox = agent_dir / "comms" / "outbox"
            if agent_outbox.is_dir():
                roots.append(agent_outbox)

    return roots


def prune_legacy_comms(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_LEGACY_COMMS_MAX_AGE_HOURS,
) -> int:
    """Sweep legacy v1 comms outboxes and v1 broadcast artifacts.

    The v2 housekeeping only prunes ``~/.skcapstone/sync/comms/outbox`` and
    never reaches the v1 layouts, so they grow unbounded. This sweeps BOTH:
    - ``<home>/comms/outbox/<recipient>/`` (v1 root path)
    - ``<home>/agents/<agent>/comms/outbox/<recipient>/`` (v1 per-agent path)

    Within each outbox it recurses one level into per-recipient subdirs
    (including a subdir whose name is literally ``*``) and deletes envelope
    files (``*.skc.json``) older than *max_age_hours*.

    Special case: a recipient subdir literally named ``*`` is a v1
    ``recipient="*"`` broadcast artifact (never valid v2). The entire dir tree
    is removed regardless of age via :func:`shutil.rmtree`, guarded against
    symlink escape and confined to the outbox dir.

    Now-empty recipient and outbox directories are removed afterward.

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Delete envelopes older than this. Default 168 (7 days).

    Returns:
        Number of files deleted.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for outbox_dir in _legacy_outbox_dirs(skcapstone_home):
        try:
            recipient_dirs = list(outbox_dir.iterdir())
        except OSError as exc:
            logger.warning("Failed to scan legacy outbox %s: %s", outbox_dir, exc)
            continue

        for recipient in recipient_dirs:
            # Never follow symlinks — stay inside skcapstone_home.
            if recipient.is_symlink():
                continue

            # v1 broadcast artifact: a subdir literally named "*". Remove whole
            # tree regardless of age (never valid v2).
            if recipient.is_dir() and recipient.name == "*":
                try:
                    file_count = sum(1 for p in recipient.rglob("*") if p.is_file())
                    shutil.rmtree(recipient)
                    deleted += file_count
                    logger.info(
                        "Removed v1 broadcast dir %s (%d files)",
                        recipient,
                        file_count,
                    )
                except OSError as exc:
                    logger.warning("Failed to remove broadcast dir %s: %s", recipient, exc)
                continue

            if not recipient.is_dir():
                continue

            for path in recipient.iterdir():
                if not path.is_file() or path.is_symlink():
                    continue
                if not path.name.endswith(".skc.json"):
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        deleted += 1
                except OSError as exc:
                    logger.warning("Failed to delete legacy envelope %s: %s", path, exc)

            # Remove now-empty recipient directory
            try:
                if recipient.is_dir() and not any(recipient.iterdir()):
                    recipient.rmdir()
            except OSError:
                pass

        # Remove now-empty outbox directory
        try:
            if outbox_dir.is_dir() and not any(outbox_dir.iterdir()):
                outbox_dir.rmdir()
        except OSError:
            pass

    if deleted:
        logger.info("Pruned %d legacy comms files from %s", deleted, skcapstone_home)
    return deleted


def _inbox_dirs(skcapstone_home: Path) -> list[Path]:
    """Return all comms-inbox roots that exist under *skcapstone_home*.

    Mirrors :func:`_legacy_outbox_dirs` but for inbox delivery targets:
    - ``<home>/comms/inbox`` — the root path.
    - ``<home>/agents/<agent>/comms/inbox`` — every per-agent path.

    Args:
        skcapstone_home: Path to ~/.skcapstone.

    Returns:
        List of existing inbox directories (may be empty).
    """
    roots: list[Path] = []

    root_inbox = skcapstone_home / "comms" / "inbox"
    if root_inbox.is_dir():
        roots.append(root_inbox)

    agents_dir = skcapstone_home / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            agent_inbox = agent_dir / "comms" / "inbox"
            if agent_inbox.is_dir():
                roots.append(agent_inbox)

    return roots


def prune_inbox(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_INBOX_MAX_AGE_HOURS,
) -> int:
    """Delete inbox envelopes older than *max_age_hours* (TTL backstop, F6).

    This is the safety net for nodes whose consumer never ran: the primary GC
    is delete-on-consume in the consciousness loop. Only ``*.skc.json`` files
    are touched (never symlinks, never other files), across both the root
    ``<home>/comms/inbox`` and every ``<home>/agents/<agent>/comms/inbox``.

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Delete envelopes older than this. Default 168 (7 days).

    Returns:
        Number of files deleted.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for inbox_dir in _inbox_dirs(skcapstone_home):
        try:
            entries = list(inbox_dir.iterdir())
        except OSError as exc:
            logger.warning("Failed to scan inbox %s: %s", inbox_dir, exc)
            continue

        for path in entries:
            if not path.is_file() or path.is_symlink():
                continue
            if not path.name.endswith(".skc.json"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete inbox envelope %s: %s", path, exc)

    if deleted:
        logger.info("Pruned %d stale inbox envelopes under %s", deleted, skcapstone_home)
    return deleted


def prune_derived_junk(skcapstone_home: Path) -> int:
    """Remove derived/runtime junk that must never sync or linger (F6/F7).

    Sweeps the whole profile tree for:
    - ``**/chroma.bak*`` — stale ChromaDB backup dumps (dirs removed wholesale
      via :func:`shutil.rmtree`, files unlinked). Live ``chroma`` is untouched.
    - ``**/*.pid`` — stale pidfiles.

    Symlinks are never followed. Best-effort; individual failures are logged and
    skipped.

    Args:
        skcapstone_home: Path to ~/.skcapstone.

    Returns:
        Number of top-level junk items removed.
    """
    if not skcapstone_home.is_dir():
        return 0

    removed = 0

    for path in skcapstone_home.rglob("chroma.bak*"):
        if path.is_symlink():
            continue
        try:
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
            else:
                continue
            removed += 1
        except OSError as exc:
            logger.warning("Failed to remove chroma backup %s: %s", path, exc)

    for path in skcapstone_home.rglob("*.pid"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Failed to remove pidfile %s: %s", path, exc)

    if removed:
        logger.info("Removed %d derived-junk items under %s", removed, skcapstone_home)
    return removed


def run_housekeeping(
    skcapstone_home: Optional[Path] = None,
    skcomms_home: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """Run all housekeeping tasks.

    The ``legacy_comms`` target's reported size/path is the v1 root outbox
    (``<home>/comms/outbox``) for display only — :func:`prune_legacy_comms`
    additionally sweeps every ``<home>/agents/<agent>/comms/outbox`` and
    removes any v1 broadcast subdir literally named ``*``.

    Args:
        skcapstone_home: Path to ~/.skcapstone. Defaults to AGENT_HOME.
        skcomms_home: Path to ~/.skcomms. Defaults to ~/.skcomms.
        dry_run: If True, report what would be deleted without deleting.

    Returns:
        Dict with counts per target and total bytes freed.
    """
    from . import AGENT_HOME

    if skcapstone_home is None:
        skcapstone_home = Path(AGENT_HOME).expanduser()
    if skcomms_home is None:
        skcomms_home = Path("~/.skcomms").expanduser()

    results: dict[str, dict] = {}

    # Measure sizes before pruning
    targets = {
        "acks": skcomms_home / "acks",
        "comms_outbox": skcapstone_home / "sync" / "comms" / "outbox",
        "seed_outbox": skcapstone_home / "sync" / "outbox",
        # Size/path reported is the v1 root outbox only; the sweep also covers
        # every agents/<agent>/comms/outbox (see prune_legacy_comms).
        "legacy_comms": skcapstone_home / "comms" / "outbox",
        # Size/path reported is the root comms/inbox only; the TTL sweep also
        # covers every agents/<agent>/comms/inbox (see prune_inbox).
        "inbox": skcapstone_home / "comms" / "inbox",
    }

    for key, path in targets.items():
        results[key] = {
            "path": str(path),
            "exists": path.is_dir(),
            "size_before": _dir_size(path),
        }

    # derived_junk is a scattered glob (chroma.bak* + *.pid), not a single dir,
    # so it is tracked outside the generic targets loop.
    results["derived_junk"] = {
        "path": str(skcapstone_home),
        "exists": skcapstone_home.is_dir(),
        "size_before": _derived_junk_size(skcapstone_home),
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
        results["legacy_comms"]["would_delete"] = _count_stale_legacy_comms(
            skcapstone_home, DEFAULT_LEGACY_COMMS_MAX_AGE_HOURS
        )
        results["inbox"]["would_delete"] = _count_stale_inbox(
            skcapstone_home, DEFAULT_INBOX_MAX_AGE_HOURS
        )
        results["derived_junk"]["would_delete"] = _count_derived_junk(skcapstone_home)
        results["dry_run"] = True
        return results

    # Actually prune
    results["acks"]["deleted"] = prune_acks(skcomms_home)
    results["comms_outbox"]["deleted"] = prune_comms_outbox(skcapstone_home / "sync")
    results["seed_outbox"]["deleted"] = prune_seeds(targets["seed_outbox"])
    results["legacy_comms"]["deleted"] = prune_legacy_comms(skcapstone_home)
    results["inbox"]["deleted"] = prune_inbox(skcapstone_home)
    results["derived_junk"]["deleted"] = prune_derived_junk(skcapstone_home)

    # Measure sizes after
    for key, path in targets.items():
        results[key]["size_after"] = _dir_size(path)
        before = results[key]["size_before"]
        after = results[key]["size_after"]
        results[key]["freed"] = max(0, before - after)

    # derived_junk size is measured over the scattered glob, not a single dir.
    dj_after = _derived_junk_size(skcapstone_home)
    results["derived_junk"]["size_after"] = dj_after
    results["derived_junk"]["freed"] = max(0, results["derived_junk"]["size_before"] - dj_after)

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
    return sum(1 for p in directory.iterdir() if p.is_file() and p.stat().st_mtime < cutoff)


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


def _count_stale_legacy_comms(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count legacy comms files that would be deleted (for dry-run).

    Mirrors :func:`prune_legacy_comms`: counts stale ``*.skc.json`` envelopes
    older than *max_age_hours* across all legacy outbox roots, plus *every*
    file under any recipient subdir literally named ``*`` (those are removed
    wholesale regardless of age).

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Age threshold matching the prune default.

    Returns:
        Number of files that would be deleted.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0

    for outbox_dir in _legacy_outbox_dirs(skcapstone_home):
        try:
            recipient_dirs = list(outbox_dir.iterdir())
        except OSError:
            continue

        for recipient in recipient_dirs:
            if recipient.is_symlink():
                continue

            if recipient.is_dir() and recipient.name == "*":
                count += sum(1 for p in recipient.rglob("*") if p.is_file())
                continue

            if not recipient.is_dir():
                continue

            for path in recipient.iterdir():
                if not path.is_file() or path.is_symlink():
                    continue
                if not path.name.endswith(".skc.json"):
                    continue
                try:
                    if path.stat().st_mtime < cutoff:
                        count += 1
                except OSError:
                    pass

    return count


def _count_stale_inbox(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count inbox envelopes that would be pruned (for dry-run).

    Mirrors :func:`prune_inbox`: counts ``*.skc.json`` files older than
    *max_age_hours* across the root and every per-agent inbox.

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Age threshold matching the prune default.

    Returns:
        Number of files that would be deleted.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0

    for inbox_dir in _inbox_dirs(skcapstone_home):
        try:
            entries = list(inbox_dir.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.is_symlink():
                continue
            if not path.name.endswith(".skc.json"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    count += 1
            except OSError:
                pass

    return count


def _count_derived_junk(skcapstone_home: Path) -> int:
    """Count derived-junk items that would be removed (for dry-run).

    Mirrors :func:`prune_derived_junk`: ``**/chroma.bak*`` entries plus
    ``**/*.pid`` files.

    Args:
        skcapstone_home: Path to ~/.skcapstone.

    Returns:
        Number of items that would be removed.
    """
    if not skcapstone_home.is_dir():
        return 0

    count = 0
    for path in skcapstone_home.rglob("chroma.bak*"):
        if not path.is_symlink():
            count += 1
    for path in skcapstone_home.rglob("*.pid"):
        if path.is_file() and not path.is_symlink():
            count += 1
    return count


def _derived_junk_size(skcapstone_home: Path) -> int:
    """Total bytes held by derived junk (chroma.bak* + *.pid), for freed calc."""
    if not skcapstone_home.is_dir():
        return 0

    total = 0
    for path in skcapstone_home.rglob("chroma.bak*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            total += _dir_size(path)
        elif path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    for path in skcapstone_home.rglob("*.pid"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total
