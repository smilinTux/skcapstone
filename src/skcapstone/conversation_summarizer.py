"""
ConversationSummarizer — LLM-powered peer conversation summarization.

Reads the last N messages from a peer conversation file, sends them to
the local LLM via LLMBridge, and stores the resulting 2-3 sentence
summary in ~/.skcapstone/summaries/{peer}.json.

Usage:
    summarizer = ConversationSummarizer(home=Path("~/.skcapstone"))
    summary = summarizer.summarize("lumina", n=20)
    print(summary.text)
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.conversation_summarizer")

# Allowlist for peer name characters (mirrors conversation_manager)
_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer_name(peer: str) -> str:
    """Sanitize peer name for safe filesystem use.

    Args:
        peer: Raw peer name.

    Returns:
        Safe peer name capped at 64 characters, or ``"unknown"``.
    """
    if not peer or not isinstance(peer, str):
        return "unknown"
    sanitized = peer.replace("\x00", "").replace("/", "").replace("\\", "")
    sanitized = _PEER_NAME_SAFE_RE.sub("", sanitized)
    sanitized = sanitized.strip(".")
    return sanitized[:64] or "unknown"


class ConversationSummary:
    """Result of a conversation summarization.

    Attributes:
        peer: The peer agent that was summarized.
        text: The 2-3 sentence summary text.
        message_count: Number of messages that were summarized.
        generated_at: UTC ISO timestamp when the summary was produced.
    """

    def __init__(self, peer: str, text: str, message_count: int, generated_at: str) -> None:
        self.peer = peer
        self.text = text
        self.message_count = message_count
        self.generated_at = generated_at

    def to_dict(self) -> dict:
        """Serialize to a plain dict for JSON storage."""
        return {
            "peer": self.peer,
            "text": self.text,
            "message_count": self.message_count,
            "generated_at": self.generated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ConversationSummary":
        """Deserialize from a stored dict."""
        return cls(
            peer=data.get("peer", ""),
            text=data.get("text", ""),
            message_count=data.get("message_count", 0),
            generated_at=data.get("generated_at", ""),
        )


class ConversationSummarizer:
    """Summarize peer conversations using the agent's LLM.

    Reads conversation history from ``{home}/conversations/{peer}.json``,
    builds a summarization prompt, calls :class:`LLMBridge`, and persists
    the result to ``{home}/summaries/{peer}.json``.

    Args:
        home: Agent home directory (e.g. ``~/.skcapstone``).
    """

    _SYSTEM_PROMPT = (
        "You are a concise summarization assistant for a sovereign agent framework. "
        "When given a conversation transcript, produce exactly 2-3 sentences that capture: "
        "(1) the main topics discussed, (2) any decisions or outcomes reached, and "
        "(3) the overall tone or relationship dynamic. "
        "Be direct and factual. Do not use bullet points or headers."
    )

    def __init__(self, home: Path) -> None:
        self._home = Path(home)
        self._conversations_dir = self._home / "conversations"
        self._summaries_dir = self._home / "summaries"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def summarize(self, peer: str, n: int = 20, bridge=None) -> ConversationSummary:
        """Summarize the last *n* messages with *peer* using the LLM.

        Reads conversation history from disk, formats the messages into a
        summarization prompt, calls :class:`LLMBridge`, and stores the
        result in ``{home}/summaries/{peer}.json``.

        Args:
            peer: Peer agent name.
            n: Maximum number of recent messages to include (default: 20).
            bridge: Optional pre-constructed :class:`LLMBridge` instance.
                    If ``None`` a fresh one is created from defaults.

        Returns:
            :class:`ConversationSummary` with the generated text.

        Raises:
            ValueError: If there are no messages to summarize.
        """
        peer = _sanitize_peer_name(peer)
        messages = self._load_messages(peer, n)

        if not messages:
            raise ValueError(f"No conversation history found for peer '{peer}'.")

        llm_bridge = bridge or self._make_bridge()
        prompt_text = self._format_prompt(peer, messages)

        try:
            from .model_router import TaskSignal
            signal = TaskSignal(
                description="Summarize a peer conversation",
                tags=["summary", "conversation"],
                estimated_tokens=len(prompt_text) // 4,
            )
            summary_text = llm_bridge.generate(
                self._SYSTEM_PROMPT, prompt_text, signal
            )
        except Exception as exc:
            logger.warning("LLM summarization failed: %s", exc)
            summary_text = f"[Summary unavailable: {exc}]"

        result = ConversationSummary(
            peer=peer,
            text=summary_text,
            message_count=len(messages),
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._persist(result)
        return result

    def load_summary(self, peer: str) -> Optional[ConversationSummary]:
        """Load the most recently stored summary for *peer*.

        Args:
            peer: Peer agent name.

        Returns:
            :class:`ConversationSummary` if one has been stored, else ``None``.
        """
        peer = _sanitize_peer_name(peer)
        path = self._summaries_dir / f"{peer}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ConversationSummary.from_dict(data)
        except Exception as exc:
            logger.debug("Failed to load summary for %s: %s", peer, exc)
            return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_messages(self, peer: str, n: int) -> list[dict]:
        """Load last *n* messages for *peer* from disk.

        Args:
            peer: Sanitized peer name.
            n: Max messages to return.

        Returns:
            List of message dicts (may be empty).
        """
        conv_file = self._conversations_dir / f"{peer}.json"
        if not conv_file.exists():
            return []
        try:
            data = json.loads(conv_file.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data[-n:] if n > 0 else data
        except Exception as exc:
            logger.debug("Failed to load conversation for %s: %s", peer, exc)
        return []

    def _format_prompt(self, peer: str, messages: list[dict]) -> str:
        """Format messages into a summarization request.

        Args:
            peer: Peer name (for context label).
            messages: List of message dicts with ``role`` and ``content``.

        Returns:
            Formatted prompt string.
        """
        lines = [f"Conversation with {peer} ({len(messages)} messages):"]
        lines.append("")
        for msg in messages:
            role = msg.get("role", "unknown")
            content = str(msg.get("content", "")).strip()
            label = "Agent" if role == "assistant" else peer.capitalize()
            lines.append(f"{label}: {content}")
        lines.append("")
        lines.append("Please summarize this conversation in 2-3 sentences.")
        return "\n".join(lines)

    def _make_bridge(self):
        """Instantiate a default :class:`LLMBridge`.

        Returns:
            A configured :class:`LLMBridge` instance.
        """
        from .consciousness_loop import ConsciousnessConfig, LLMBridge
        config = ConsciousnessConfig()
        return LLMBridge(config)

    def _persist(self, summary: ConversationSummary) -> None:
        """Write summary to ``{home}/summaries/{peer}.json``.

        Uses a temp file + rename for atomic updates.

        Args:
            summary: The summary to persist.
        """
        try:
            self._summaries_dir.mkdir(parents=True, exist_ok=True)
            target = self._summaries_dir / f"{summary.peer}.json"
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except Exception as exc:
            logger.warning("Failed to persist summary for %s: %s", summary.peer, exc)
