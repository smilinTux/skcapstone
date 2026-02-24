"""
Session auto-capture — the agent never forgets a conversation.

Extracts key moments from AI conversations, scores importance by
topic novelty and information density, and stores each as a tagged
memory. Tool-agnostic: works with Claude Code, Cursor, Windsurf,
or any tool that can pass conversation text.

Usage:
    # Via CLI
    skcapstone session capture "We decided to use Ed25519 for all agent keys"
    skcapstone session capture --file transcript.txt
    echo "discussion notes" | skcapstone session capture --stdin

    # Via MCP tool
    session_capture(content="...", tags=["architecture"])

    # Via Python
    from skcapstone.session_capture import SessionCapture
    cap = SessionCapture(home)
    cap.capture("We decided to use Ed25519...")
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .memory_engine import search, store
from .models import MemoryEntry


@dataclass
class CapturedMoment:
    """A single extracted moment from a conversation.

    Attributes:
        content: The distilled text of the moment.
        importance: Auto-scored importance 0.0-1.0.
        tags: Auto-generated tags from content analysis.
        reason: Why this moment was scored as it was.
    """

    content: str
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)
    reason: str = ""


# Patterns that signal high-importance content
_HIGH_SIGNAL_PATTERNS: list[tuple[re.Pattern, float, str]] = [
    (re.compile(r"\bdecid", re.I), 0.2, "decision"),
    (re.compile(r"\bchose|\bpicked|\bselect", re.I), 0.15, "decision"),
    (re.compile(r"\barchitecture|\bdesign\b|\bpattern", re.I), 0.15, "architecture"),
    (re.compile(r"\bbug\b|\bfix\b|\bissue\b|\berror\b", re.I), 0.1, "bugfix"),
    (re.compile(r"\bsecur|\bencrypt|\bPGP\b|\bGPG\b|\bkey\b", re.I), 0.15, "security"),
    (re.compile(r"\bAPI\b|\bendpoint|\bschema\b", re.I), 0.1, "api"),
    (re.compile(r"\bdeploy|\brelease|\bpublish", re.I), 0.1, "deployment"),
    (re.compile(r"\bTODO\b|\bFIXME\b|\bHACK\b", re.I), 0.1, "todo"),
    (re.compile(r"\bimportant|\bcritical|\bmust\b|\brequir", re.I), 0.15, "priority"),
    (re.compile(r"\bnever\b|\balways\b|\brule\b|\bconvention", re.I), 0.1, "convention"),
]

# Patterns for auto-tagging
_TAG_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bcapauth\b", re.I), "capauth"),
    (re.compile(r"\bskcapstone\b", re.I), "skcapstone"),
    (re.compile(r"\bskmemory\b", re.I), "skmemory"),
    (re.compile(r"\bskcomm\b", re.I), "skcomm"),
    (re.compile(r"\bskchat\b", re.I), "skchat"),
    (re.compile(r"\bsyncthing\b", re.I), "syncthing"),
    (re.compile(r"\bMCP\b", re.I), "mcp"),
    (re.compile(r"\bPGP\b|\bGPG\b", re.I), "pgp"),
    (re.compile(r"\bDocker\b", re.I), "docker"),
    (re.compile(r"\bPython\b", re.I), "python"),
    (re.compile(r"\btest\b", re.I), "testing"),
]

_SENTENCE_SPLITTER = re.compile(r"(?<=[.!?])\s+|\n\n+|\n(?=[A-Z#\-\*])")


class SessionCapture:
    """Captures AI conversation content as sovereign memories.

    Extracts key moments, auto-scores importance, deduplicates
    against existing memories, and stores to the agent's memory.

    Args:
        home: Agent home directory (~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self.home = home

    def capture(
        self,
        content: str,
        tags: Optional[list[str]] = None,
        source: str = "session",
        min_importance: float = 0.3,
    ) -> list[MemoryEntry]:
        """Capture conversation content as memories.

        Splits content into moments, scores each, deduplicates,
        and stores those above the minimum importance threshold.

        Args:
            content: Raw conversation text (any length).
            tags: Additional tags to apply to all captured memories.
            source: Memory source identifier.
            min_importance: Minimum importance to store (0.0-1.0).

        Returns:
            List of stored MemoryEntry objects.
        """
        moments = self.extract_moments(content)
        scored = [self.score_moment(m) for m in moments]
        filtered = [m for m in scored if m.importance >= min_importance]
        deduped = self._deduplicate(filtered)

        extra_tags = tags or []
        stored: list[MemoryEntry] = []

        for moment in deduped:
            all_tags = list(set(["session-capture"] + moment.tags + extra_tags))
            entry = store(
                home=self.home,
                content=moment.content,
                tags=all_tags,
                source=source,
                importance=moment.importance,
                metadata={"capture_reason": moment.reason},
            )
            stored.append(entry)

        return stored

    def extract_moments(self, content: str) -> list[str]:
        """Split conversation content into distinct moments.

        A moment is a meaningful unit: a paragraph, a decision,
        a key statement. Short fragments are merged with neighbors.

        Args:
            content: Raw text to split.

        Returns:
            List of moment strings.
        """
        content = content.strip()
        if not content:
            return []

        segments = _SENTENCE_SPLITTER.split(content)
        moments: list[str] = []
        buffer = ""

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue

            if len(buffer) + len(seg) < 60:
                buffer = f"{buffer} {seg}".strip() if buffer else seg
            else:
                if buffer:
                    moments.append(buffer)
                buffer = seg

        if buffer:
            moments.append(buffer)

        return [m for m in moments if len(m) >= 20]

    def score_moment(self, text: str) -> CapturedMoment:
        """Score a moment's importance and auto-tag it.

        Scoring is based on signal patterns (decisions, architecture,
        security mentions, etc.) and content density (longer, more
        specific content scores higher).

        Args:
            text: A single moment string.

        Returns:
            CapturedMoment with importance score and tags.
        """
        base_score = 0.3
        reasons: list[str] = []
        tags: list[str] = []

        for pattern, boost, label in _HIGH_SIGNAL_PATTERNS:
            if pattern.search(text):
                base_score += boost
                reasons.append(label)

        for pattern, tag in _TAG_PATTERNS:
            if pattern.search(text):
                tags.append(tag)

        # Reason: longer, denser content tends to be more informative
        word_count = len(text.split())
        if word_count > 30:
            base_score += 0.05
        if word_count > 60:
            base_score += 0.05

        importance = min(1.0, base_score)
        reason = ", ".join(reasons) if reasons else "general"

        return CapturedMoment(
            content=text,
            importance=round(importance, 2),
            tags=tags,
            reason=reason,
        )

    def _deduplicate(self, moments: list[CapturedMoment]) -> list[CapturedMoment]:
        """Remove moments that are too similar to existing memories.

        Uses content hashing for exact dedup and search overlap
        for semantic-ish dedup.

        Args:
            moments: Scored moments to deduplicate.

        Returns:
            Deduplicated list of moments.
        """
        seen_hashes: set[str] = set()
        unique: list[CapturedMoment] = []

        for m in moments:
            h = hashlib.md5(m.content.lower().encode()).hexdigest()[:12]
            if h in seen_hashes:
                continue
            seen_hashes.add(h)

            existing = search(self.home, m.content[:50], limit=1)
            if existing and _text_overlap(m.content, existing[0].content) > 0.7:
                continue

            unique.append(m)

        return unique


def _text_overlap(a: str, b: str) -> float:
    """Compute word-level Jaccard overlap between two strings.

    Args:
        a: First string.
        b: Second string.

    Returns:
        Overlap ratio 0.0-1.0.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)
