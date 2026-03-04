"""
ContextWindowManager — per-sender token tracking and history compression.

Tracks cumulative token usage for each sender's conversation history.
When a sender's history reaches 80% of ``max_context_tokens``, the oldest
messages are summarised into a single paragraph by the LLM and replaced in
the ConversationStore, keeping only the most recent ``_KEEP_RECENT``
messages verbatim.

Token counting: uses ``tiktoken`` (cl100k_base) when installed, otherwise
falls back to ``len(content) // 4`` (the same char-based estimate used
throughout the rest of skcapstone).

Usage (inside ConsciousnessLoop)::

    ctx_mgr = ContextWindowManager(home, config.max_context_tokens)
    # After storing a new assistant reply:
    ctx_mgr.check_and_compress(sender, conv_store, bridge)
    # Via MCP tool:
    stats = ctx_mgr.get_all_stats(conv_store)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .conversation_store import ConversationStore

logger = logging.getLogger("skcapstone.context_window")

# System prompt for the compression LLM call
_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a concise summarization assistant for a sovereign AI agent framework. "
    "Summarize the following conversation history into exactly one paragraph (3-5 sentences). "
    "Capture: the main topics discussed, any decisions or outcomes reached, and the overall tone. "
    "This summary replaces older messages to free up context window space. "
    "Be factual and direct. Do not use bullet points or headers."
)

# How many recent messages to keep verbatim (not included in summarization)
_KEEP_RECENT = 4

# Context window fill threshold that triggers compression (80 %)
_THRESHOLD_PCT = 0.80


# ---------------------------------------------------------------------------
# Token helpers (module-level, reusable)
# ---------------------------------------------------------------------------


def count_tokens(text: str) -> int:
    """Count tokens in *text*.

    Uses ``tiktoken`` (cl100k_base encoding) when the package is installed.
    Falls back to ``max(1, len(text) // 4)`` (4 chars ≈ 1 token) otherwise.

    Args:
        text: Input text.

    Returns:
        Token count (always >= 1 for non-empty input).
    """
    try:
        import tiktoken  # optional dep
        enc = tiktoken.get_encoding("cl100k_base")
        return max(1, len(enc.encode(text)))
    except ImportError:
        return max(1, len(text) // 4)


def count_history_tokens(history: list[dict]) -> int:
    """Sum token counts for all messages in a history list.

    Args:
        history: List of message dicts, each expected to have a ``"content"`` key.

    Returns:
        Total token estimate across all messages.
    """
    return sum(count_tokens(str(msg.get("content", ""))) for msg in history)


# ---------------------------------------------------------------------------
# ContextWindowManager
# ---------------------------------------------------------------------------


class ContextWindowManager:
    """Tracks per-sender token usage and compresses history at the 80 % threshold.

    Maintains an in-memory stats table for every peer that has been checked.
    Stats are refreshed on every :meth:`check_and_compress` call and after a
    successful compression.

    Args:
        home: Agent home directory (used for any future persistence needs).
        max_context_tokens: Model context window token budget.  The
            compression threshold is set to 80 % of this value.
    """

    def __init__(self, home: Path, max_context_tokens: int = 8000) -> None:
        self._home = Path(home)
        self._max_context_tokens = max_context_tokens
        self._threshold = int(max_context_tokens * _THRESHOLD_PCT)
        # peer -> stats snapshot
        self._stats: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_and_compress(
        self,
        peer: str,
        store: "ConversationStore",
        bridge=None,
    ) -> bool:
        """Check peer history token count; compress if at or over threshold.

        Loads the full history for *peer* from *store*, counts tokens, and
        updates the in-memory stats table.  When the count meets or exceeds
        the 80 % threshold and *bridge* is provided, the oldest messages are
        summarised by the LLM (keeping ``_KEEP_RECENT`` verbatim) and the
        history is atomically replaced on disk via
        :meth:`ConversationStore.replace`.

        Args:
            peer: Sanitised peer name.
            store: :class:`~skcapstone.conversation_store.ConversationStore`
                instance for reading/writing history.
            bridge: :class:`~skcapstone.consciousness_loop.LLMBridge` used to
                generate the summary.  If ``None`` compression is skipped but
                stats are still updated.

        Returns:
            ``True`` if the history was compressed, ``False`` otherwise.
        """
        history = store.load(peer)
        token_count = count_history_tokens(history)

        self._stats[peer] = {
            "tokens": token_count,
            "messages": len(history),
            "threshold": self._threshold,
            "max_context_tokens": self._max_context_tokens,
            "pct_used": round(token_count / self._max_context_tokens * 100, 1),
            "last_compressed_at": self._stats.get(peer, {}).get("last_compressed_at"),
        }

        if token_count < self._threshold:
            return False

        if bridge is None:
            logger.warning(
                "Context window at %.1f%% for %s but no bridge — skipping compression",
                self._stats[peer]["pct_used"],
                peer,
            )
            return False

        if len(history) <= _KEEP_RECENT:
            logger.debug(
                "Context window at %.1f%% for %s but only %d messages — skipping",
                self._stats[peer]["pct_used"],
                peer,
                len(history),
            )
            return False

        to_summarize = history[:-_KEEP_RECENT]
        recent = history[-_KEEP_RECENT:]

        logger.info(
            "Context window %.1f%% for %s — compressing %d older messages",
            self._stats[peer]["pct_used"],
            peer,
            len(to_summarize),
        )

        summary_text = self._call_llm_summarize(peer, to_summarize, bridge)
        if not summary_text:
            logger.warning("LLM summarization returned empty result for %s — skipping", peer)
            return False

        now = datetime.now(timezone.utc).isoformat()
        summary_entry: dict = {
            "role": "system",
            "content": (
                f"[Earlier context — {len(to_summarize)} messages summarized]: {summary_text}"
            ),
            "timestamp": now,
            "is_summary": True,
            "summarized_count": len(to_summarize),
        }
        new_history = [summary_entry] + recent
        store.replace(peer, new_history)

        new_token_count = count_history_tokens(new_history)
        self._stats[peer].update(
            {
                "tokens": new_token_count,
                "messages": len(new_history),
                "pct_used": round(new_token_count / self._max_context_tokens * 100, 1),
                "last_compressed_at": now,
            }
        )
        logger.info(
            "Context compressed for %s: %d→%d messages, %d→%d tokens (%.1f%%)",
            peer,
            len(history),
            len(new_history),
            token_count,
            new_token_count,
            self._stats[peer]["pct_used"],
        )
        return True

    def update_stats(self, peer: str, store: "ConversationStore") -> dict:
        """Refresh and return stats for *peer* without triggering compression.

        Args:
            peer: Peer name.
            store: :class:`~skcapstone.conversation_store.ConversationStore`.

        Returns:
            Stats dict for this peer.
        """
        history = store.load(peer)
        token_count = count_history_tokens(history)
        self._stats[peer] = {
            "tokens": token_count,
            "messages": len(history),
            "threshold": self._threshold,
            "max_context_tokens": self._max_context_tokens,
            "pct_used": round(token_count / self._max_context_tokens * 100, 1),
            "last_compressed_at": self._stats.get(peer, {}).get("last_compressed_at"),
        }
        return self._stats[peer]

    def get_all_stats(
        self, store: Optional["ConversationStore"] = None
    ) -> dict[str, dict]:
        """Return current stats for all tracked senders.

        When *store* is provided any peers that have on-disk history but are
        not yet in the in-memory stats table (e.g. written by a previous
        process) are lazily loaded and included.

        Args:
            store: Optional :class:`~skcapstone.conversation_store.ConversationStore`
                used to discover and load previously unseen peers.

        Returns:
            Mapping of peer name → stats dict.
        """
        if store is not None:
            for peer in store.all_peers():
                if peer not in self._stats:
                    self.update_stats(peer, store)
        return dict(self._stats)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_llm_summarize(
        self, peer: str, messages: list[dict], bridge
    ) -> str:
        """Call *bridge* to produce a one-paragraph summary of *messages*.

        Args:
            peer: Peer name (included in the summarisation prompt for context).
            messages: Older messages to summarise.
            bridge: :class:`~skcapstone.consciousness_loop.LLMBridge`.

        Returns:
            Summary string, or empty string on any failure.
        """
        try:
            from .model_router import TaskSignal

            lines = [f"Conversation with {peer} ({len(messages)} messages to summarize):"]
            lines.append("")
            for msg in messages:
                role = msg.get("role", "unknown")
                content = str(msg.get("content", "")).strip()
                # Skip existing summary sentinels (nested compression guard)
                if msg.get("is_summary"):
                    lines.append(f"[Previous summary]: {content}")
                    continue
                label = "Agent" if role == "assistant" else peer.capitalize()
                lines.append(f"{label}: {content}")
            lines.append("")
            lines.append(
                "Summarize the above into one paragraph (3-5 sentences). "
                "Preserve key topics, decisions, and tone."
            )
            prompt_text = "\n".join(lines)

            signal = TaskSignal(
                description="Compress conversation context window",
                tags=["summary", "context"],
                estimated_tokens=count_tokens(prompt_text),
            )
            result = bridge.generate(_SUMMARIZE_SYSTEM_PROMPT, prompt_text, signal)
            return result or ""
        except Exception as exc:
            logger.warning("Context compression LLM call failed for %s: %s", peer, exc)
            return ""
