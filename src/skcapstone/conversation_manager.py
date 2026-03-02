"""
ConversationManager — centralized manager for all peer conversation histories.

Owns the {home}/conversations/ directory. Provides a clean API for adding,
retrieving, searching, and exporting conversations instead of ad-hoc file
writes scattered across the codebase.

Used by ConsciousnessLoop (via SystemPromptBuilder) and daemon API endpoints.
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.conversation_manager")

# Allowlist for peer name characters (alphanumeric + safe punctuation)
_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer_name(peer: str) -> str:
    """Sanitize a peer name for safe use as a filesystem key.

    Strips path separators (/ \\), null bytes, and any character not in the
    alphanumeric + ``-_@.`` set. Caps length at 64 characters. Returns
    ``"unknown"`` if the result would be empty.

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


class ConversationManager:
    """Centralized manager for all peer conversation histories.

    Stores conversations as JSON files under {home}/conversations/{peer}.json.
    Provides atomic writes, in-memory caching, search, and export.

    Args:
        home: Agent home directory (~/.skcapstone).
        max_history_messages: Maximum messages to retain per peer in memory
            and on disk.
    """

    def __init__(self, home: Path, max_history_messages: int = 10) -> None:
        self._home = Path(home)
        self._conversations_dir = self._home / "conversations"
        self._max_history_messages = max_history_messages
        self._history: dict[str, list[dict]] = defaultdict(list)
        self._load_all()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_peers(self) -> list[dict]:
        """List all peers that have conversation history.

        Returns:
            List of summary dicts, each with keys:
            ``peer``, ``message_count``, ``last_message_time``,
            ``last_message_preview``. Sorted most-recent-first.
        """
        peers = []
        for peer, messages in self._history.items():
            if not messages:
                continue
            last = messages[-1]
            peers.append({
                "peer": peer,
                "message_count": len(messages),
                "last_message_time": last.get("timestamp"),
                "last_message_preview": last.get("content", "")[:80],
            })
        peers.sort(key=lambda p: p["last_message_time"] or "", reverse=True)
        return peers

    def get_history(self, peer: str) -> list[dict]:
        """Get full conversation history for a peer.

        Args:
            peer: Peer agent name.

        Returns:
            List of message dicts with ``role``, ``content``, ``timestamp``.
            Returns an empty list if the peer is unknown.
        """
        peer = _sanitize_peer_name(peer)
        return list(self._history.get(peer, []))

    def add_message(self, peer: str, role: str, content: str) -> dict:
        """Add a message to the peer's conversation history.

        Appends to in-memory history, caps at ``max_history_messages``, and
        atomically persists to disk.

        Args:
            peer: Peer agent name.
            role: ``"user"`` or ``"assistant"``.
            content: Message content.

        Returns:
            The message dict that was stored (includes ``timestamp``).
        """
        peer = _sanitize_peer_name(peer)
        msg: dict = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._history[peer].append(msg)
        if len(self._history[peer]) > self._max_history_messages:
            self._history[peer] = self._history[peer][-self._max_history_messages:]
        self._persist(peer)
        return msg

    def search(self, query: str) -> list[dict]:
        """Search for a query string across all conversation histories.

        Case-insensitive substring match against message content.

        Args:
            query: Search string.

        Returns:
            List of match dicts, each with ``peer``, ``role``, ``content``,
            ``timestamp``.
        """
        query_lower = query.lower()
        matches: list[dict] = []
        for peer, messages in self._history.items():
            for msg in messages:
                if query_lower in msg.get("content", "").lower():
                    matches.append({
                        "peer": peer,
                        "role": msg.get("role"),
                        "content": msg.get("content"),
                        "timestamp": msg.get("timestamp"),
                    })
        return matches

    def export_all(self) -> dict[str, list[dict]]:
        """Export all conversations as a plain dict.

        Returns:
            Dict mapping peer name → list of message dicts.
            Peers with no messages are excluded.
        """
        return {peer: list(msgs) for peer, msgs in self._history.items() if msgs}

    def delete(self, peer: str) -> bool:
        """Delete a peer's conversation history from memory and disk.

        Args:
            peer: Peer agent name.

        Returns:
            ``True`` if the conversation existed and was deleted.
        """
        peer = _sanitize_peer_name(peer)
        existed = bool(self._history.pop(peer, None))
        target = self._conversations_dir / f"{peer}.json"
        if target.exists():
            target.unlink()
            return True
        return existed

    def format_history_for_prompt(self, peer: str, max_messages: int = 10) -> str:
        """Format recent conversation history for inclusion in a system prompt.

        Args:
            peer: Peer agent name.
            max_messages: Maximum messages to include.

        Returns:
            Formatted history string, or empty string if no history.
        """
        history = self._history.get(peer, [])
        if not history:
            return ""
        recent = history[-max_messages:]
        lines = [f"Recent conversation with {peer}:"]
        for msg in recent:
            role = msg.get("role", "?")
            content = msg.get("content", "")[:200]
            lines.append(f"  [{role}] {content}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_all(self) -> None:
        """Load all peer conversation files from the conversations directory."""
        if not self._conversations_dir.exists():
            return
        for conv_file in self._conversations_dir.glob("*.json"):
            peer = conv_file.stem
            try:
                data = json.loads(conv_file.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._history[peer] = data[-self._max_history_messages:]
            except Exception as exc:
                logger.debug("Failed to load conversation %s: %s", conv_file, exc)

    def _persist(self, peer: str) -> None:
        """Atomically write peer history to {home}/conversations/{peer}.json.

        Uses a temp file + rename for atomic update, preventing corruption if
        the process is interrupted mid-write.

        Args:
            peer: Peer agent name (already sanitized).
        """
        try:
            self._conversations_dir.mkdir(parents=True, exist_ok=True)
            target = self._conversations_dir / f"{peer}.json"
            tmp = target.with_suffix(".json.tmp")
            payload = json.dumps(self._history[peer], ensure_ascii=False, indent=2)
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:
            logger.debug("Failed to persist conversation for %s: %s", peer, exc)
