"""
ConversationStore — focused, stateless per-peer conversation history.

Stores conversations as JSON arrays in {home}/conversations/{peer}.json.
Each entry: {"role": str, "content": str, "timestamp": ISO-8601}.

Unlike ConversationManager this module is stateless — every call reads
from / writes to disk directly, making it suitable for CLI tools and
processes that need to see the latest on-disk state rather than a
snapshot loaded at init time.

Compatible with ConversationManager: both use the same JSON file format
so files written by one can be read by the other without migration.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.conversation_store")

# Allowlist for peer name characters (alphanumeric + safe punctuation)
_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer_name(peer: str) -> str:
    """Sanitize a peer name for safe use as a filesystem key.

    Strips path separators, null bytes, and any character not in the
    alphanumeric + ``-_@.`` set.  Caps length at 64 characters.
    Returns ``"unknown"`` if the result would be empty.

    Args:
        peer: Raw peer name.

    Returns:
        Filesystem-safe peer name, at most 64 characters long.
    """
    if not peer or not isinstance(peer, str):
        return "unknown"
    sanitized = peer.replace("\x00", "").replace("/", "").replace("\\", "")
    sanitized = _PEER_NAME_SAFE_RE.sub("", sanitized)
    sanitized = sanitized.strip(".")
    return sanitized[:64] or "unknown"


class ConversationStore:
    """Lightweight, stateless per-peer conversation history store.

    Reads and writes ``{home}/conversations/{peer}.json`` files directly
    with atomic rename-based updates.  No in-memory caching — every call
    reflects the current on-disk state.

    Compatible with :class:`~skcapstone.conversation_manager.ConversationManager`:
    both use the same JSON list format, so files are interchangeable.

    Args:
        home: Agent home directory (e.g. ``~/.skcapstone``).
    """

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._dir = self._home / "conversations"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def append(
        self,
        peer: str,
        role: str,
        content: str,
        *,
        thread_id: Optional[str] = None,
        in_reply_to: Optional[str] = None,
    ) -> dict:
        """Append one message to the peer's history file.

        Reads the existing file (if any), appends the new entry, and
        atomically writes back.  Creates the file and parent directory
        if absent.

        Args:
            peer: Peer agent name.
            role: ``"user"`` or ``"assistant"``.
            content: Message text.
            thread_id: Optional thread identifier for grouping messages.
            in_reply_to: Optional message ID this message replies to.

        Returns:
            The message dict that was stored (always includes
            ``role``, ``content``, and ``timestamp``).
        """
        peer = _sanitize_peer_name(peer)
        msg: dict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if thread_id:
            msg["thread_id"] = thread_id
        if in_reply_to:
            msg["in_reply_to"] = in_reply_to

        history = self._read(peer)
        history.append(msg)
        self._write(peer, history)
        return msg

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_last(self, peer: str, n: int = 10) -> list[dict]:
        """Return the last *n* messages for a peer.

        Args:
            peer: Peer agent name.
            n: Maximum number of messages to return (default 10).

        Returns:
            List of message dicts, oldest first.  Empty list if peer
            unknown or *n* is zero.
        """
        peer = _sanitize_peer_name(peer)
        history = self._read(peer)
        return history[-n:] if n > 0 else []

    def load(self, peer: str) -> list[dict]:
        """Return the full conversation history for a peer.

        Args:
            peer: Peer agent name.

        Returns:
            List of all stored message dicts, oldest first.
        """
        peer = _sanitize_peer_name(peer)
        return self._read(peer)

    def all_peers(self) -> list[str]:
        """Return names of all peers that have a saved conversation file.

        Returns:
            Sorted list of peer names (file stems without extension).
        """
        if not self._dir.exists():
            return []
        return sorted(p.stem for p in self._dir.glob("*.json"))

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def clear(self, peer: str) -> bool:
        """Delete a peer's conversation file.

        Args:
            peer: Peer agent name.

        Returns:
            ``True`` if the file existed and was deleted, ``False``
            if the peer had no saved history.
        """
        peer = _sanitize_peer_name(peer)
        target = self._dir / f"{peer}.json"
        if target.exists():
            target.unlink()
            return True
        return False

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def format_for_prompt(self, peer: str, n: int = 10) -> str:
        """Format the last *n* messages as a prompt-ready string.

        Args:
            peer: Peer agent name.
            n: Maximum messages to include.

        Returns:
            Human-readable history block, or empty string if no history.
        """
        messages = self.get_last(peer, n)
        if not messages:
            return ""
        lines = [f"Recent conversation with {peer}:"]
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")[:200]
            lines.append(f"  [{role}] {content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _read(self, peer: str) -> list[dict]:
        """Read the peer's conversation file, returning [] on any error."""
        target = self._dir / f"{peer}.json"
        if not target.exists():
            return []
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to read conversation for %s: %s", peer, exc)
            return []

    def _write(self, peer: str, history: list[dict]) -> None:
        """Atomically write *history* to the peer's conversation file."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            target = self._dir / f"{peer}.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(target)
        except Exception as exc:
            logger.debug("Failed to write conversation for %s: %s", peer, exc)
