"""Conversation archiver — moves old peer messages to compressed archives.

Reads active conversation files from {home}/conversations/{peer}.json,
archives messages older than ``age_days`` days (default 30) that are
not within the last ``keep_recent`` messages (default 100), and writes
them to gzip-compressed JSON files under {home}/archive/{peer}.json.gz.

The active file is rewritten with only the retained messages.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.archiver")


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ArchiveResult:
    """Summary of a single-peer archival operation.

    Attributes:
        peer: Peer name.
        archived_count: Number of messages moved to the archive.
        retained_count: Number of messages kept in the active file.
        archive_path: Path of the (possibly updated) archive file.
        skipped: True if the peer had no messages to archive.
    """

    peer: str
    archived_count: int = 0
    retained_count: int = 0
    archive_path: Optional[Path] = None
    skipped: bool = False


@dataclass
class ArchiveSummary:
    """Aggregate result of archiving all peers.

    Attributes:
        peers_processed: Peers whose files were examined.
        peers_skipped: Peers with nothing to archive.
        total_archived: Total messages moved to archives.
        total_retained: Total messages kept in active files.
        results: Per-peer ArchiveResult list.
    """

    peers_processed: int = 0
    peers_skipped: int = 0
    total_archived: int = 0
    total_retained: int = 0
    results: list[ArchiveResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _parse_ts(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime.

    Args:
        ts: ISO-8601 string, or None.

    Returns:
        Timezone-aware datetime, or None if parsing fails.
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _load_messages(path: Path) -> list[dict]:
    """Load a JSON conversation file, returning an empty list on any error.

    Args:
        path: Path to the .json conversation file.

    Returns:
        List of message dicts.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("Failed to load %s: %s", path, exc)
        return []


def _save_messages(path: Path, messages: list[dict]) -> None:
    """Atomically write messages to a JSON file.

    Args:
        path: Target file path.
        messages: List of message dicts.
    """
    tmp = path.with_suffix(".json.tmp")
    payload = json.dumps(messages, ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def _load_archive(path: Path) -> list[dict]:
    """Load messages from a gzip-compressed JSON archive file.

    Args:
        path: Path to the .json.gz archive file.

    Returns:
        List of message dicts; empty list if file absent or corrupt.
    """
    if not path.exists():
        return []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except Exception as exc:
        logger.debug("Failed to load archive %s: %s", path, exc)
        return []


def _save_archive(path: Path, messages: list[dict]) -> None:
    """Write messages to a gzip-compressed JSON archive file.

    Sorts messages by timestamp before writing. Uses a tmp file + rename
    for atomic update.

    Args:
        path: Target archive file (.json.gz).
        messages: List of message dicts to persist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    messages.sort(key=lambda m: m.get("timestamp") or "")
    tmp = path.with_suffix(".gz.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=9) as fh:
        json.dump(messages, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Main archiver class
# ---------------------------------------------------------------------------


class ConversationArchiver:
    """Archives old peer conversation messages to compressed files.

    Keeps the last ``keep_recent`` messages in the active conversation
    file; messages that are both older than ``age_days`` days *and* not
    within the last ``keep_recent`` are moved to the peer's archive file
    at ``{archive_dir}/{peer}.json.gz``.

    Args:
        home: Agent home directory (~/.skcapstone).
        age_days: Minimum age in days for a message to be archivable.
        keep_recent: Always retain this many most-recent messages in the
            active file, regardless of their age.
        archive_dir: Override the default archive directory.
    """

    def __init__(
        self,
        home: Path,
        *,
        age_days: int = 30,
        keep_recent: int = 100,
        archive_dir: Optional[Path] = None,
    ) -> None:
        self._home = Path(home)
        self._conversations_dir = self._home / "conversations"
        self._archive_dir = Path(archive_dir) if archive_dir else self._home / "archive"
        self._age_days = age_days
        self._keep_recent = keep_recent

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def archive_peer(self, peer: str) -> ArchiveResult:
        """Archive old messages for a single peer.

        Reads the active conversation file, partitions messages into
        *retain* and *archive* sets, rewrites the active file with the
        retained set, and appends the archived messages to the peer's
        gzip archive.

        Args:
            peer: Sanitised peer name (used as filename stem).

        Returns:
            ArchiveResult describing what happened.
        """
        active_path = self._conversations_dir / f"{peer}.json"
        if not active_path.exists():
            return ArchiveResult(peer=peer, skipped=True)

        messages = _load_messages(active_path)
        if not messages:
            return ArchiveResult(peer=peer, skipped=True)

        retain, to_archive = self._partition(messages)

        if not to_archive:
            return ArchiveResult(
                peer=peer,
                skipped=True,
                retained_count=len(retain),
            )

        # Merge with existing archive and write
        archive_path = self._archive_dir / f"{peer}.json.gz"
        existing = _load_archive(archive_path)
        merged = existing + to_archive
        _save_archive(archive_path, merged)

        # Rewrite active file with only retained messages
        _save_messages(active_path, retain)

        logger.info(
            "peer=%s archived=%d retained=%d",
            peer,
            len(to_archive),
            len(retain),
        )
        return ArchiveResult(
            peer=peer,
            archived_count=len(to_archive),
            retained_count=len(retain),
            archive_path=archive_path,
        )

    def archive_all(self) -> ArchiveSummary:
        """Archive old messages for all peers in the conversations directory.

        Returns:
            ArchiveSummary with per-peer results and totals.
        """
        summary = ArchiveSummary()

        if not self._conversations_dir.exists():
            return summary

        for conv_file in sorted(self._conversations_dir.glob("*.json")):
            peer = conv_file.stem
            result = self.archive_peer(peer)
            summary.results.append(result)
            summary.peers_processed += 1
            if result.skipped:
                summary.peers_skipped += 1
            else:
                summary.total_archived += result.archived_count
                summary.total_retained += result.retained_count

        return summary

    def list_archives(self) -> list[dict]:
        """List all archive files in the archive directory.

        Returns:
            List of dicts with ``peer``, ``path``, ``size_bytes``,
            ``message_count``. Sorted by peer name.
        """
        if not self._archive_dir.exists():
            return []

        archives = []
        for gz_file in sorted(self._archive_dir.glob("*.json.gz")):
            peer = gz_file.stem.removesuffix(".json") if gz_file.stem.endswith(".json") else gz_file.stem
            messages = _load_archive(gz_file)
            archives.append({
                "peer": peer,
                "path": gz_file,
                "size_bytes": gz_file.stat().st_size,
                "message_count": len(messages),
            })
        return archives

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _partition(self, messages: list[dict]) -> tuple[list[dict], list[dict]]:
        """Split messages into (retain, archive) lists.

        A message is archived only if *both* conditions hold:
        1. Its timestamp is older than ``age_days`` days.
        2. It is not among the ``keep_recent`` most-recent messages.

        Messages with unparseable timestamps are always retained to avoid
        data loss.

        Args:
            messages: Full list of messages for a peer.

        Returns:
            Tuple of (retain_list, archive_list).
        """
        if not messages:
            return [], []

        cutoff = datetime.now(timezone.utc) - timedelta(days=self._age_days)

        # Indices of the last keep_recent messages are always retained
        protected_indices = set(range(max(0, len(messages) - self._keep_recent), len(messages)))

        retain: list[dict] = []
        to_archive: list[dict] = []

        for i, msg in enumerate(messages):
            if i in protected_indices:
                retain.append(msg)
                continue
            ts = _parse_ts(msg.get("timestamp"))
            if ts is None or ts >= cutoff:
                # Can't determine age, or not old enough — keep it
                retain.append(msg)
            else:
                to_archive.append(msg)

        return retain, to_archive
