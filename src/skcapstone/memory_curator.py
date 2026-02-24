"""
Memory Curator — analyze, score, tag, promote, and deduplicate memories.

Runs a curation pass over the agent's memory store, identifying:
- Promotion candidates (short->mid, mid->long based on access/importance)
- Missing tags (auto-tags from content analysis)
- Duplicate/near-duplicate memories to merge
- Importance re-scoring based on current context
- Summary statistics for each memory layer

Tool-agnostic: works from any terminal, MCP, or the REPL shell.

Usage:
    skcapstone memory curate              # full curation pass
    skcapstone memory curate --dry-run    # preview without changes
    skcapstone memory curate --promote    # only run promotions
    skcapstone memory curate --dedupe     # only run deduplication
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .memory_engine import (
    _entry_path,
    _find_by_id,
    _save_entry,
    list_memories,
    search,
)
from .models import MemoryEntry, MemoryLayer


@dataclass
class CurationResult:
    """Results from a curation pass.

    Attributes:
        promoted: Memories promoted to a higher tier.
        tagged: Memories that received new auto-tags.
        deduped: Memory IDs that were identified as duplicates.
        total_scanned: Total memories examined.
        by_layer: Count per layer after curation.
    """

    promoted: list[str] = field(default_factory=list)
    tagged: list[str] = field(default_factory=list)
    deduped: list[str] = field(default_factory=list)
    total_scanned: int = 0
    by_layer: dict[str, int] = field(default_factory=dict)


# Auto-tagging patterns (same as session_capture for consistency)
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
    (re.compile(r"\barchitect", re.I), "architecture"),
    (re.compile(r"\bdecid", re.I), "decision"),
    (re.compile(r"\bsecur|\bencrypt", re.I), "security"),
    (re.compile(r"\bdeploy|\brelease", re.I), "deployment"),
]


class MemoryCurator:
    """Curates the agent's memory store.

    Runs analysis passes to improve memory quality: auto-tagging,
    promotion candidates, deduplication, and importance re-scoring.

    Args:
        home: Agent home directory (~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self.home = home

    def curate(
        self,
        dry_run: bool = False,
        promote: bool = True,
        dedupe: bool = True,
        auto_tag: bool = True,
    ) -> CurationResult:
        """Run a full curation pass.

        Args:
            dry_run: If True, report changes without applying them.
            promote: Run the promotion pass.
            dedupe: Run the deduplication pass.
            auto_tag: Run the auto-tagging pass.

        Returns:
            CurationResult with all changes made (or proposed).
        """
        result = CurationResult()
        all_memories = list_memories(self.home, limit=10000)
        result.total_scanned = len(all_memories)

        for layer in MemoryLayer:
            count = sum(1 for m in all_memories if m.layer == layer)
            result.by_layer[layer.value] = count

        if auto_tag:
            self._pass_auto_tag(all_memories, result, dry_run)

        if promote:
            self._pass_promote(all_memories, result, dry_run)

        if dedupe:
            self._pass_dedupe(all_memories, result, dry_run)

        return result

    def _pass_auto_tag(
        self, memories: list[MemoryEntry], result: CurationResult, dry_run: bool
    ) -> None:
        """Add missing tags based on content analysis."""
        for entry in memories:
            new_tags = _suggest_tags(entry.content, entry.tags)
            if new_tags:
                if not dry_run:
                    entry.tags = list(set(entry.tags + new_tags))
                    _save_entry(self.home, entry)
                result.tagged.append(entry.memory_id)

    def _pass_promote(
        self, memories: list[MemoryEntry], result: CurationResult, dry_run: bool
    ) -> None:
        """Promote memories that qualify for a higher tier."""
        for entry in memories:
            if not entry.should_promote:
                continue

            old_layer = entry.layer
            if not dry_run:
                old_path = _entry_path(self.home, entry)
                if entry.layer == MemoryLayer.SHORT_TERM:
                    entry.layer = MemoryLayer.MID_TERM
                elif entry.layer == MemoryLayer.MID_TERM:
                    entry.layer = MemoryLayer.LONG_TERM

                if old_path.exists():
                    old_path.unlink()
                _save_entry(self.home, entry)

            result.promoted.append(entry.memory_id)

    def _pass_dedupe(
        self, memories: list[MemoryEntry], result: CurationResult, dry_run: bool
    ) -> None:
        """Identify and remove near-duplicate memories."""
        seen: dict[str, str] = {}

        sorted_memories = sorted(
            memories,
            key=lambda m: (
                {"long-term": 0, "mid-term": 1, "short-term": 2}.get(m.layer.value, 3),
                -m.importance,
                -m.access_count,
            ),
        )

        for entry in sorted_memories:
            content_hash = _content_hash(entry.content)

            if content_hash in seen:
                result.deduped.append(entry.memory_id)
                if not dry_run:
                    path = _entry_path(self.home, entry)
                    if path.exists():
                        path.unlink()
                continue

            seen[content_hash] = entry.memory_id

    def get_stats(self) -> dict:
        """Get curation-oriented statistics.

        Returns:
            Dict with layer counts, tag coverage, and quality metrics.
        """
        all_memories = list_memories(self.home, limit=10000)
        total = len(all_memories)
        if total == 0:
            return {"total": 0, "layers": {}, "tag_coverage": 0.0, "promotion_candidates": 0}

        by_layer = {}
        for layer in MemoryLayer:
            by_layer[layer.value] = sum(1 for m in all_memories if m.layer == layer)

        tagged = sum(1 for m in all_memories if m.tags)
        promotable = sum(1 for m in all_memories if m.should_promote)
        avg_importance = sum(m.importance for m in all_memories) / total

        top_tags: dict[str, int] = {}
        for m in all_memories:
            for t in m.tags:
                top_tags[t] = top_tags.get(t, 0) + 1

        sorted_tags = sorted(top_tags.items(), key=lambda x: -x[1])[:15]

        return {
            "total": total,
            "layers": by_layer,
            "tag_coverage": round(tagged / total, 2),
            "avg_importance": round(avg_importance, 2),
            "promotion_candidates": promotable,
            "top_tags": sorted_tags,
        }


def _suggest_tags(content: str, existing_tags: list[str]) -> list[str]:
    """Suggest new tags based on content analysis.

    Args:
        content: Memory content text.
        existing_tags: Already-applied tags.

    Returns:
        List of new tag suggestions (not already in existing_tags).
    """
    suggestions: list[str] = []
    existing_set = set(existing_tags)

    for pattern, tag in _TAG_PATTERNS:
        if tag not in existing_set and pattern.search(content):
            suggestions.append(tag)

    return suggestions


def _content_hash(content: str) -> str:
    """Generate a normalized hash for deduplication.

    Normalizes whitespace and case before hashing to catch
    near-identical content.

    Args:
        content: Memory content text.

    Returns:
        MD5 hex digest of the normalized content.
    """
    normalized = " ".join(content.lower().split())
    return hashlib.md5(normalized.encode()).hexdigest()[:16]
