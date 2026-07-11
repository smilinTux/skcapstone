"""
Housekeeping — storage pruning for the sovereign agent.

Prunes stale files that accumulate in the agent profile:
- ACK files in ~/.skcapstone/skcomms/acks/ (age-based, 24h default) — the
  canonical skcomms home (SKCOMMS_HOME default), NOT the dead ~/.skcomms path
- Delivered envelopes in ~/.skcapstone/sync/comms/outbox/ (age-based, 48h)
- Seed snapshots in ~/.skcapstone/sync/outbox/ (count-based, keep 10)
- Legacy v1 comms outboxes (age-based, 7d) — both the root path
  ~/.skcapstone/comms/outbox/<recipient>/ and every per-agent path
  ~/.skcapstone/agents/<agent>/comms/outbox/<recipient>/, plus any v1
  broadcast subdir literally named ``*`` (removed wholesale)
- Runtime comms junk (age-based): the skcomms mailbox/federation inbox
  ~/.skcapstone/skcomms/inbox/ (72h), consumed-message archives in every
  comms/archive tree (48h), and FLAT ~/.skcapstone/agents/<agent>/comms/outbox
  envelopes that prune_legacy_comms's subdir-only sweep never reached (48h)

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
# Deadletter TTL: quarantined envelopes are kept for inspection but must not
# grow unbounded (they replicate to every peer). Backstop only.
DEFAULT_DEADLETTER_MAX_AGE_HOURS = 168  # 7 days
# skcomms mailbox/federation inbox ({home}/skcomms/inbox) is largely STATIC but
# accumulates (266k observed). Conservative TTL — these are delivered/consumed
# mailbox envelopes; comfortably past any consume latency at 72h.
DEFAULT_SKCOMMS_INBOX_MAX_AGE_HOURS = 72  # 3 days
# Consumed messages archived by FileTransport.receive into comms/archive
# (agents/<agent>/comms/archive + {home}/comms/archive, ~170k observed). These
# are ALREADY delivered — safe to reclaim at a short TTL.
DEFAULT_COMMS_ARCHIVE_MAX_AGE_HOURS = 48  # 2 days
# Live v2 per-agent comms outbox flat envelopes (agents/<agent>/comms/outbox,
# ~54k observed). A shared-filesystem write is a delivered queue hand-off; by
# 48h it has long since synced + been consumed. Tighter than the 7d legacy TTL
# because these are live, high-churn outboxes (not long-dead v1 data).
DEFAULT_OUTBOX_FLAT_MAX_AGE_HOURS = 48  # 2 days


def _pid_is_alive(pidfile: Path) -> bool:
    """Return True if *pidfile* names a live process (must be preserved).

    A pidfile is considered NOT alive (safe to delete) if it is unreadable,
    empty, non-integer, names a non-positive PID, or names a PID that no longer
    exists (``ProcessLookupError`` from ``os.kill(pid, 0)``). A PID that exists
    but is owned by another user raises ``PermissionError`` — that still proves
    the process is alive, so it is preserved.

    Args:
        pidfile: Path to a ``*.pid`` file.

    Returns:
        True if the named process is alive; False otherwise.
    """
    try:
        raw = pidfile.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not raw:
        return False
    try:
        pid = int(raw)
    except ValueError:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # no such process — stale pidfile
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


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


def _prune_skc_tree_by_ttl(root: Path, max_age_hours: int, label: str) -> int:
    """Recursively delete ``*.skc.json`` files older than *max_age_hours* under *root*.

    Symlink-safe (symlinked files skipped; ``rglob`` does not descend into
    symlinked directories) and dotfile/.tmp-safe (``*.skc.json`` never matches a
    ``.tmp`` file, and leading-dot names are skipped explicitly).

    Args:
        root: Directory tree to sweep.
        max_age_hours: Delete envelopes older than this.
        label: Human label for the log line.

    Returns:
        Number of files deleted.
    """
    if not root.is_dir():
        return 0

    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for path in root.rglob("*.skc.json"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith("."):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as exc:
            logger.warning("Failed to delete %s envelope %s: %s", label, path, exc)

    if deleted:
        logger.info("Pruned %d stale %s envelopes under %s", deleted, label, root)
    return deleted


def prune_inbox(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_INBOX_MAX_AGE_HOURS,
) -> int:
    """Delete inbox envelopes older than *max_age_hours* (TTL backstop, F3/F6).

    This is the safety net for nodes whose consumer never ran: the primary GC
    is delete-on-consume in the consciousness loop. It targets the exact tree
    the consciousness loop consumes — ``{shared_root}/sync/comms/inbox`` — and
    sweeps it RECURSIVELY (per-peer ``inbox/<peer>/*.skc.json`` subdirs).

    It deliberately does NOT touch ``agents/<agent>/comms/inbox``: that is
    SKCHAT's inbox, consumed by a different service (skchat/skcomms owns its
    lifecycle via ``FileTransport.prune_inbox``). Only ``*.skc.json`` files are
    removed (never symlinks, dotfiles, or other files).

    Args:
        skcapstone_home: Shared root (``~/.skcapstone``); the sweep runs under
            its ``sync/comms/inbox`` subtree.
        max_age_hours: Delete envelopes older than this. Default 168 (7 days).

    Returns:
        Number of files deleted.
    """
    inbox_root = skcapstone_home / "sync" / "comms" / "inbox"
    return _prune_skc_tree_by_ttl(inbox_root, max_age_hours, "inbox")


def prune_deadletter(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_DEADLETTER_MAX_AGE_HOURS,
) -> int:
    """Delete deadletter envelopes older than *max_age_hours* (F5).

    Quarantined (malformed/oversized/poison) envelopes are moved to
    ``{shared_root}/sync/comms/deadletter`` by the consciousness loop. They are
    kept for inspection but replicate to every peer, so an unbounded deadletter
    tree bloats the whole cluster. This TTL backstop reclaims them. Recursive,
    symlink-safe, ``*.skc.json`` only.

    Args:
        skcapstone_home: Shared root (``~/.skcapstone``).
        max_age_hours: Delete envelopes older than this. Default 168 (7 days).

    Returns:
        Number of files deleted.
    """
    dead_root = skcapstone_home / "sync" / "comms" / "deadletter"
    return _prune_skc_tree_by_ttl(dead_root, max_age_hours, "deadletter")


def prune_derived_junk(skcapstone_home: Path) -> int:
    """Remove derived/runtime junk that must never sync or linger (F6/F7).

    Sweeps the whole profile tree for:
    - ``**/chroma.bak*`` — stale ChromaDB backup dumps (dirs removed wholesale
      via :func:`shutil.rmtree`, files unlinked). Live ``chroma`` is untouched.
    - ``**/*.pid`` — pidfiles whose process is NOT alive. A pidfile naming a
      live PID (e.g. the running daemon's own ``daemon.pid``) is preserved;
      only dead / empty / garbage pidfiles are removed.

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
        # NEVER delete a live daemon's pidfile — only stale/garbage ones.
        if _pid_is_alive(path):
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Failed to remove pidfile %s: %s", path, exc)

    if removed:
        logger.info("Removed %d derived-junk items under %s", removed, skcapstone_home)
    return removed


def prune_skcomms_inbox(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_SKCOMMS_INBOX_MAX_AGE_HOURS,
) -> int:
    """TTL-prune the skcomms mailbox/federation inbox tree.

    Targets ``{skcapstone_home}/skcomms/inbox`` — the canonical skcomms home
    (``skcomms.home.skcomms_home()`` defaults to ``~/.skcapstone/skcomms``).
    The tree is swept RECURSIVELY: per-peer mailbox subdirs
    (``inbox/<peer>/*.skc.json``) and the bulk ``inbox/archive`` subtree.

    This inbox is largely static but accumulates unbounded (266k files
    observed on a live node), pinning the Syncthing scanner. These are
    delivered/consumed mailbox envelopes, so a conservative TTL reclaims them
    safely. Only ``*.skc.json`` files are removed (never symlinks/dotfiles).

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Delete envelopes older than this. Default 72 (3 days).

    Returns:
        Number of files deleted.
    """
    inbox_root = skcapstone_home / "skcomms" / "inbox"
    return _prune_skc_tree_by_ttl(inbox_root, max_age_hours, "skcomms_inbox")


def prune_comms_archive(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_COMMS_ARCHIVE_MAX_AGE_HOURS,
) -> int:
    """TTL-prune consumed-message archives (FileTransport.receive archive).

    ``FileTransport.receive`` archives every consumed inbox file into
    ``comms/archive``. Nothing ever deletes them, so they grow unbounded
    (~170k observed). They are ALREADY-CONSUMED messages — safe to reclaim.

    Sweeps BOTH:
    - ``{home}/comms/archive`` (root path)
    - every ``{home}/agents/<agent>/comms/archive`` (per-agent path)

    Each root is swept recursively; only ``*.skc.json`` files older than
    *max_age_hours* are removed (never symlinks/dotfiles).

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Delete archived envelopes older than this. Default 48.

    Returns:
        Number of files deleted.
    """
    deleted = 0
    deleted += _prune_skc_tree_by_ttl(
        skcapstone_home / "comms" / "archive", max_age_hours, "comms_archive"
    )

    agents_dir = skcapstone_home / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if not agent_dir.is_dir():
                continue
            deleted += _prune_skc_tree_by_ttl(
                agent_dir / "comms" / "archive", max_age_hours, "comms_archive"
            )
    return deleted


def prune_comms_outbox_flat(
    skcapstone_home: Path,
    max_age_hours: int = DEFAULT_OUTBOX_FLAT_MAX_AGE_HOURS,
) -> int:
    """TTL-prune FLAT envelopes directly in the live v2 comms outboxes.

    The live v2 file transport writes envelopes FLAT into each outbox
    (``agents/<agent>/comms/outbox/<id>.skc.json``), but
    :func:`prune_legacy_comms` only reaches files nested one level down inside
    per-recipient SUBDIRS. So the flat outbox files (~54k observed) were never
    pruned. This closes that gap.

    It reuses :func:`_legacy_outbox_dirs` to enumerate the outbox roots
    (``{home}/comms/outbox`` + every ``agents/<agent>/comms/outbox``) and
    deletes only FLAT ``*.skc.json`` files directly in each root — it does NOT
    descend into per-recipient subdirs (those stay owned by
    :func:`prune_legacy_comms`), so the two never double-handle a file.

    A shared-filesystem write is a delivered queue hand-off; by *max_age_hours*
    (48h default, tighter than the 7d legacy TTL) it has long since synced and
    been consumed. In-flight ``.tmp`` / leading-dot files are skipped.

    Args:
        skcapstone_home: Path to ~/.skcapstone.
        max_age_hours: Delete envelopes older than this. Default 48 (2 days).

    Returns:
        Number of files deleted.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    deleted = 0

    for outbox_dir in _legacy_outbox_dirs(skcapstone_home):
        try:
            entries = list(outbox_dir.iterdir())
        except OSError as exc:
            logger.warning("Failed to scan outbox %s: %s", outbox_dir, exc)
            continue

        for path in entries:
            # Only flat envelope files; recipient subdirs belong to legacy prune.
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.startswith("."):
                continue
            if not path.name.endswith(".skc.json"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    deleted += 1
            except OSError as exc:
                logger.warning("Failed to delete outbox envelope %s: %s", path, exc)

    if deleted:
        logger.info("Pruned %d flat comms-outbox envelopes under %s", deleted, skcapstone_home)
    return deleted


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
        # The canonical skcomms home is ~/.skcapstone/skcomms (skcomms.home
        # .skcomms_home() default, honoring SKCOMMS_HOME) — NOT ~/.skcomms.
        # The old ~/.skcomms default meant prune_acks swept a dead path while
        # the real acks piled up at {home}/skcomms/acks (179k observed). Derive
        # from skcapstone_home so a custom shared_root stays consistent.
        env_home = os.environ.get("SKCOMMS_HOME")
        skcomms_home = (
            Path(env_home).expanduser() if env_home else skcapstone_home / "skcomms"
        )

    results: dict[str, dict] = {}

    # Measure sizes before pruning
    targets = {
        "acks": skcomms_home / "acks",
        "comms_outbox": skcapstone_home / "sync" / "comms" / "outbox",
        "seed_outbox": skcapstone_home / "sync" / "outbox",
        # Size/path reported is the v1 root outbox only; the sweep also covers
        # every agents/<agent>/comms/outbox (see prune_legacy_comms).
        "legacy_comms": skcapstone_home / "comms" / "outbox",
        # The inbox/deadletter TTL sweeps run under sync/comms (the tree the
        # consciousness loop actually consumes / quarantines to), recursively.
        "inbox": skcapstone_home / "sync" / "comms" / "inbox",
        "deadletter": skcapstone_home / "sync" / "comms" / "deadletter",
        # Runtime comms junk (F: comms-runtime-junk). The skcomms mailbox inbox
        # is a single tree; comms_archive/comms_outbox_flat report the ROOT path
        # for display only — their sweeps also cover every agents/<agent>/comms/*
        # (see the respective prune functions).
        "skcomms_inbox": skcapstone_home / "skcomms" / "inbox",
        "comms_archive": skcapstone_home / "comms" / "archive",
        "comms_outbox_flat": skcapstone_home / "comms" / "outbox",
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
        results["deadletter"]["would_delete"] = _count_stale_deadletter(
            skcapstone_home, DEFAULT_DEADLETTER_MAX_AGE_HOURS
        )
        results["skcomms_inbox"]["would_delete"] = _count_stale_skc_tree(
            skcapstone_home / "skcomms" / "inbox", DEFAULT_SKCOMMS_INBOX_MAX_AGE_HOURS
        )
        results["comms_archive"]["would_delete"] = _count_stale_comms_archive(
            skcapstone_home, DEFAULT_COMMS_ARCHIVE_MAX_AGE_HOURS
        )
        results["comms_outbox_flat"]["would_delete"] = _count_stale_outbox_flat(
            skcapstone_home, DEFAULT_OUTBOX_FLAT_MAX_AGE_HOURS
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
    results["deadletter"]["deleted"] = prune_deadletter(skcapstone_home)
    results["skcomms_inbox"]["deleted"] = prune_skcomms_inbox(skcapstone_home)
    results["comms_archive"]["deleted"] = prune_comms_archive(skcapstone_home)
    results["comms_outbox_flat"]["deleted"] = prune_comms_outbox_flat(skcapstone_home)
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


def _count_stale_skc_tree(root: Path, max_age_hours: int) -> int:
    """Count ``*.skc.json`` files older than *max_age_hours* under *root* (dry-run)."""
    if not root.is_dir():
        return 0
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0
    for path in root.rglob("*.skc.json"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.startswith("."):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                count += 1
        except OSError:
            pass
    return count


def _count_stale_inbox(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count inbox envelopes that would be pruned (for dry-run).

    Mirrors :func:`prune_inbox`: recursive ``*.skc.json`` under
    ``sync/comms/inbox`` older than *max_age_hours*.
    """
    return _count_stale_skc_tree(skcapstone_home / "sync" / "comms" / "inbox", max_age_hours)


def _count_stale_deadletter(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count deadletter envelopes that would be pruned (for dry-run).

    Mirrors :func:`prune_deadletter`: recursive ``*.skc.json`` under
    ``sync/comms/deadletter`` older than *max_age_hours*.
    """
    return _count_stale_skc_tree(skcapstone_home / "sync" / "comms" / "deadletter", max_age_hours)


def _count_stale_comms_archive(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count consumed-archive envelopes that would be pruned (for dry-run).

    Mirrors :func:`prune_comms_archive`: recursive ``*.skc.json`` under
    ``{home}/comms/archive`` plus every ``agents/<agent>/comms/archive``.
    """
    count = _count_stale_skc_tree(skcapstone_home / "comms" / "archive", max_age_hours)
    agents_dir = skcapstone_home / "agents"
    if agents_dir.is_dir():
        for agent_dir in agents_dir.iterdir():
            if agent_dir.is_dir():
                count += _count_stale_skc_tree(
                    agent_dir / "comms" / "archive", max_age_hours
                )
    return count


def _count_stale_outbox_flat(skcapstone_home: Path, max_age_hours: int) -> int:
    """Count flat outbox envelopes that would be pruned (for dry-run).

    Mirrors :func:`prune_comms_outbox_flat`: FLAT ``*.skc.json`` files directly
    in each outbox root (``{home}/comms/outbox`` + every
    ``agents/<agent>/comms/outbox``), NOT descending into recipient subdirs.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0
    for outbox_dir in _legacy_outbox_dirs(skcapstone_home):
        try:
            entries = list(outbox_dir.iterdir())
        except OSError:
            continue
        for path in entries:
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.startswith("."):
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
        if path.is_file() and not path.is_symlink() and not _pid_is_alive(path):
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
        if _pid_is_alive(path):
            continue  # live pidfile is not junk — do not count its bytes as freeable
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total
