"""
Unified Search — full-text search across all agent data stores.

Searches memories, conversations, and SKComm messages in one query.
Results are ranked by a combined relevance + recency score.

Data stores searched:
    memories     — ~/.skcapstone/memory/{short,mid,long}-term/*.json
    conversations — ~/.skcapstone/conversations/*.json
    messages     — ~/.skcapstone/sync/comms/archive/*.skc.json
    journal      — ~/.skcapstone/journal/*.json  (if present)
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.unified_search")

# Score decay: 1 point per match, multiplied by recency weight (0–1).
# Recency weight = 1 / (1 + age_days * RECENCY_DECAY)
RECENCY_DECAY = 0.05  # ~14-day half-life
MEMORY_LAYER_BOOST = {"long-term": 1.5, "mid-term": 1.2, "short-term": 1.0}


@dataclass
class SearchResult:
    """A single result from the unified search."""

    source: str  # "memory", "conversation", "message", "journal"
    result_id: str  # Unique identifier within the source
    title: str  # Human-readable label (peer name, memory ID, etc.)
    preview: str  # Short text snippet showing the match context
    score: float  # Composite relevance + recency score
    timestamp: Optional[datetime]  # When the item was created/last modified
    metadata: dict = field(default_factory=dict)  # Source-specific extras


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _recency_weight(ts: Optional[datetime]) -> float:
    """Compute a recency weight in [0, 1] from an ISO timestamp.

    Newer items score closer to 1.0; older items decay toward 0.

    Args:
        ts: UTC-aware datetime, or None.

    Returns:
        Float in (0, 1].
    """
    if ts is None:
        return 0.5  # unknown age — neutral weight
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - ts).total_seconds() / 86_400)
    return 1.0 / (1.0 + age_days * RECENCY_DECAY)


def _count_matches(pattern: re.Pattern, *texts: str) -> int:
    """Count total regex matches across one or more text strings.

    Args:
        pattern: Compiled case-insensitive regex.
        *texts: Strings to search.

    Returns:
        Total match count.
    """
    return sum(len(pattern.findall(t)) for t in texts if t)


def _snippet(text: str, pattern: re.Pattern, window: int = 80) -> str:
    """Extract a context snippet around the first match.

    Args:
        text: Source text to extract from.
        pattern: Compiled regex to locate.
        window: Characters to show on each side of the match.

    Returns:
        A clipped snippet string.
    """
    m = pattern.search(text)
    if m is None:
        return text[: window * 2]
    start = max(0, m.start() - window)
    end = min(len(text), m.end() + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 datetime string, returning None on failure.

    Args:
        value: ISO-8601 string or None.

    Returns:
        Parsed datetime or None.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Per-source search functions
# ---------------------------------------------------------------------------


def _search_memories(
    home: Path,
    pattern: re.Pattern,
) -> list[SearchResult]:
    """Search the three-tier memory store.

    Args:
        home: Agent home directory.
        pattern: Compiled search pattern.

    Returns:
        List of SearchResult objects from the memory store.
    """
    results: list[SearchResult] = []
    from . import active_agent_name

    agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name()
    if home.parent.name == "agents":
        # home is already an agent-specific dir (e.g. ~/.skcapstone/agents/lumina)
        mem_dir = home / "memory"
    elif agent_name:
        mem_dir = home / "agents" / agent_name / "memory"
    else:
        mem_dir = home / "memory"
    if not mem_dir.exists():
        return results

    for layer_name in ("long-term", "mid-term", "short-term"):
        layer_dir = mem_dir / layer_name
        if not layer_dir.exists():
            continue
        for f in layer_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("Skipping memory %s: %s", f, exc)
                continue

            content = data.get("content", "")
            tags = " ".join(data.get("tags", []))
            matches = _count_matches(pattern, content, tags)
            if matches == 0:
                continue

            ts = _parse_dt(data.get("created_at"))
            importance = float(data.get("importance", 0.5))
            layer_boost = MEMORY_LAYER_BOOST.get(layer_name, 1.0)
            score = matches * importance * layer_boost * _recency_weight(ts)

            memory_id = data.get("memory_id", f.stem)
            preview = _snippet(content, pattern)
            tag_str = ", ".join(data.get("tags", [])) if data.get("tags") else ""

            results.append(
                SearchResult(
                    source="memory",
                    result_id=memory_id,
                    title=f"{memory_id} [{layer_name}]",
                    preview=preview,
                    score=score,
                    timestamp=ts,
                    metadata={
                        "layer": layer_name,
                        "importance": importance,
                        "tags": tag_str,
                        "source": data.get("source", ""),
                    },
                )
            )

    return results


def _search_conversations(
    home: Path,
    pattern: re.Pattern,
) -> list[SearchResult]:
    """Search conversation history files.

    Each conversation is stored as a list of {role, content, timestamp}
    dicts in ~/.skcapstone/conversations/<peer>.json.

    Args:
        home: Agent home directory.
        pattern: Compiled search pattern.

    Returns:
        List of SearchResult objects from conversations.
    """
    results: list[SearchResult] = []
    conv_dir = home / "conversations"
    if not conv_dir.exists():
        return results

    for f in conv_dir.glob("*.json"):
        try:
            messages = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Skipping conversation %s: %s", f, exc)
            continue

        if not isinstance(messages, list):
            continue

        peer = f.stem
        for idx, msg in enumerate(messages):
            content = msg.get("content", "")
            matches = _count_matches(pattern, content)
            if matches == 0:
                continue

            ts = _parse_dt(msg.get("timestamp"))
            score = matches * _recency_weight(ts)
            role = msg.get("role", "?")

            results.append(
                SearchResult(
                    source="conversation",
                    result_id=f"{peer}:{idx}",
                    title=f"Conversation with {peer} [{role}]",
                    preview=_snippet(content, pattern),
                    score=score,
                    timestamp=ts,
                    metadata={"peer": peer, "role": role, "message_index": idx},
                )
            )

    return results


def _search_messages(
    home: Path,
    pattern: re.Pattern,
) -> list[SearchResult]:
    """Search archived SKComm envelope files (.skc.json).

    Handles both the legacy schema (payload.text) and the newer
    schema (payload.content).

    Args:
        home: Agent home directory.
        pattern: Compiled search pattern.

    Returns:
        List of SearchResult objects from SKComm messages.
    """
    results: list[SearchResult] = []

    # Locations where .skc.json files may live
    search_dirs = [
        home / "sync" / "comms" / "archive",
        home / "comms" / "archive",
    ]

    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        for f in base_dir.glob("*.skc.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("Skipping message %s: %s", f, exc)
                continue

            payload = data.get("payload", {})
            # Support both field names used across schema versions
            text = payload.get("text") or payload.get("content", "")
            metadata_block = data.get("metadata", {})

            matches = _count_matches(pattern, text)
            if matches == 0:
                continue

            # Prefer envelope-level timestamp, fall back to metadata
            ts_raw = data.get("created_at") or metadata_block.get("created_at")
            ts = _parse_dt(ts_raw)
            score = matches * _recency_weight(ts)

            envelope_id = data.get("id") or data.get("envelope_id", f.stem)
            sender = data.get("from_peer") or data.get("sender", "?")
            recipient = data.get("to_peer") or data.get("recipient", "?")

            results.append(
                SearchResult(
                    source="message",
                    result_id=str(envelope_id),
                    title=f"Message {sender} → {recipient}",
                    preview=_snippet(text, pattern),
                    score=score,
                    timestamp=ts,
                    metadata={
                        "sender": sender,
                        "recipient": recipient,
                        "file": f.name,
                    },
                )
            )

    return results


def _search_journal(
    home: Path,
    pattern: re.Pattern,
) -> list[SearchResult]:
    """Search journal entries in ~/.skcapstone/journal/*.json.

    Journal entries are expected to have at least {content, created_at}.
    Missing directory is silently skipped.

    Args:
        home: Agent home directory.
        pattern: Compiled search pattern.

    Returns:
        List of SearchResult objects from journal entries.
    """
    results: list[SearchResult] = []
    journal_dir = home / "journal"
    if not journal_dir.exists():
        return results

    for f in journal_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Skipping journal %s: %s", f, exc)
            continue

        content = data.get("content", "") or data.get("text", "")
        title_text = data.get("title", "")
        matches = _count_matches(pattern, content, title_text)
        if matches == 0:
            continue

        ts = _parse_dt(data.get("created_at"))
        score = matches * _recency_weight(ts)

        results.append(
            SearchResult(
                source="journal",
                result_id=f.stem,
                title=f"Journal: {title_text or f.stem}",
                preview=_snippet(content, pattern),
                score=score,
                timestamp=ts,
                metadata={"file": f.name},
            )
        )

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

SOURCE_ALL = frozenset({"memory", "conversation", "message", "journal"})


def search(
    home: Path,
    query: str,
    sources: Optional[frozenset[str]] = None,
    limit: int = 20,
) -> list[SearchResult]:
    """Search across all agent data stores.

    Performs full-text regex matching against memories, conversations,
    SKComm messages, and journal entries. Results are ranked by a
    combined relevance × recency score, highest first.

    Args:
        home: Agent home directory (usually ~/.skcapstone).
        query: Search query. Treated as a literal string (not regex).
        sources: Set of source names to include. Defaults to all sources.
            Valid values: "memory", "conversation", "message", "journal".
        limit: Maximum number of results to return.

    Returns:
        List of SearchResult objects, ranked by score descending.
    """
    if not query.strip():
        return []

    active_sources = sources if sources is not None else SOURCE_ALL
    pattern = re.compile(re.escape(query.strip()), re.IGNORECASE)

    all_results: list[SearchResult] = []

    if "memory" in active_sources:
        all_results.extend(_search_memories(home, pattern))
    if "conversation" in active_sources:
        all_results.extend(_search_conversations(home, pattern))
    if "message" in active_sources:
        all_results.extend(_search_messages(home, pattern))
    if "journal" in active_sources:
        all_results.extend(_search_journal(home, pattern))

    all_results.sort(key=lambda r: r.score, reverse=True)
    return all_results[:limit]
