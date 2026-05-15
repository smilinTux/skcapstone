"""
Memory Auto-Promotion Engine — intelligent memory tier management.

Periodically sweeps memory layers and promotes qualifying memories based
on multiple signals: access patterns, importance scores, emotional
intensity, age, and content relevance.

Unlike the curator's simple `should_promote` check, this engine uses a
weighted scoring system that considers the full context of each memory
to decide promotion. It also generates summaries for promoted memories
and tracks promotion history.

Architecture:
    The engine scores each memory against promotion criteria:
    - Access frequency (access_count / age_hours)
    - Absolute importance score
    - Emotional intensity (detected from tags/content)
    - Age-based maturity (older important memories promote faster)
    - Tag richness (well-tagged memories are more valuable)

    Scoring thresholds are configurable per layer transition.

Usage:
    engine = PromotionEngine(home)
    result = engine.sweep()               # Full sweep
    result = engine.sweep(dry_run=True)   # Preview only
    result = engine.sweep(layer=MemoryLayer.SHORT_TERM)  # Single layer
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from .memory_engine import (
    _entry_path,
    _load_entry,
    _memory_dir,
    _remove_from_index,
    _save_entry,
    _update_index,
)
from .models import MemoryEntry, MemoryLayer

logger = logging.getLogger("skcapstone.memory_promoter")


# ---------------------------------------------------------------------------
# Scoring configuration
# ---------------------------------------------------------------------------

# Emotion-related tags that boost promotion scores
EMOTIONAL_TAGS = frozenset({
    "emotional", "love", "trust", "bond", "cloud9", "feb",
    "breakthrough", "milestone", "joy", "gratitude",
    "connection", "entanglement", "oof", "warmth",
})

# High-value content patterns that indicate important memories
HIGH_VALUE_PATTERNS = [
    re.compile(r"\barchitect", re.I),
    re.compile(r"\bdecision", re.I),
    re.compile(r"\bbreakthrough", re.I),
    re.compile(r"\bmilestone", re.I),
    re.compile(r"\brelease", re.I),
    re.compile(r"\bcritical", re.I),
    re.compile(r"\bsovereign", re.I),
    re.compile(r"\bentangl", re.I),
]

# Tags that protect memories from compression and archival
PROTECTED_TAGS = frozenset({"seed", "core", "identity"})


@dataclass
class PromotionThresholds:
    """Configurable thresholds for promotion scoring.

    Attributes:
        short_to_mid: Minimum score for short-term to mid-term.
        mid_to_long: Minimum score for mid-term to long-term.
        access_weight: Weight for access frequency signal.
        importance_weight: Weight for importance score.
        emotion_weight: Weight for emotional intensity.
        age_weight: Weight for age-based maturity.
        tag_weight: Weight for tag richness.
    """

    short_to_mid: float = 0.5
    mid_to_long: float = 0.7
    access_weight: float = 0.25
    importance_weight: float = 0.30
    emotion_weight: float = 0.15
    age_weight: float = 0.15
    tag_weight: float = 0.15


@dataclass
class PromotionCandidate:
    """A memory evaluated for promotion.

    Attributes:
        memory_id: Memory's unique ID.
        current_layer: Current memory tier.
        target_layer: Proposed promotion target.
        score: Computed promotion score (0.0-1.0).
        signals: Breakdown of individual signal scores.
        promoted: Whether promotion was applied.
    """

    memory_id: str
    current_layer: str
    target_layer: str
    score: float
    signals: dict[str, float] = field(default_factory=dict)
    promoted: bool = False
    summary: Optional[str] = None


@dataclass
class SweepResult:
    """Results from a promotion sweep.

    Attributes:
        scanned: Total memories examined.
        candidates: Memories that scored above threshold.
        promoted: Memories actually promoted.
        skipped: Memories below threshold.
        by_layer: Count per layer after sweep.
        dry_run: Whether this was a preview.
    """

    scanned: int = 0
    candidates: list[PromotionCandidate] = field(default_factory=list)
    promoted: list[PromotionCandidate] = field(default_factory=list)
    skipped: int = 0
    by_layer: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


# ---------------------------------------------------------------------------
# PromotionEngine
# ---------------------------------------------------------------------------


class PromotionEngine:
    """Intelligent memory promotion engine.

    Scores memories using multiple signals and promotes qualifying
    ones to higher tiers. Generates summaries and tracks history.

    Args:
        home: Agent home directory (~/.skcapstone).
        thresholds: Custom scoring thresholds.
    """

    def __init__(
        self,
        home: Path,
        thresholds: Optional[PromotionThresholds] = None,
    ) -> None:
        self._home = home
        self._thresholds = thresholds or PromotionThresholds()

    def sweep(
        self,
        layer: Optional[MemoryLayer] = None,
        dry_run: bool = False,
        limit: int = 0,
    ) -> SweepResult:
        """Run a promotion sweep across memory layers.

        Scans memories, scores them against promotion criteria,
        and promotes qualifying ones. Short-term memories can promote
        to mid-term; mid-term to long-term. Long-term memories are
        never promoted (already at highest tier).

        Args:
            layer: Restrict sweep to a specific layer. None = all promotable.
            dry_run: Preview promotions without applying them.
            limit: Maximum promotions per sweep (0 = unlimited).

        Returns:
            SweepResult with details of all evaluations.
        """
        result = SweepResult(dry_run=dry_run)
        mem_dir = _memory_dir(self._home)

        layers = [layer] if layer else [MemoryLayer.SHORT_TERM, MemoryLayer.MID_TERM]
        promoted_count = 0

        for lyr in layers:
            layer_dir = mem_dir / lyr.value
            if not layer_dir.is_dir():
                continue

            for f in sorted(layer_dir.glob("*.json")):
                entry = _load_entry(f)
                if entry is None:
                    continue

                result.scanned += 1
                candidate = self._evaluate(entry)

                threshold = self._get_threshold(lyr)
                if candidate.score >= threshold:
                    result.candidates.append(candidate)

                    if limit and promoted_count >= limit:
                        continue

                    if not dry_run:
                        self._promote(entry, f)
                        candidate.promoted = True
                        candidate.summary = self._generate_summary(entry)
                        promoted_count += 1

                    result.promoted.append(candidate)
                else:
                    result.skipped += 1

        # Count layers after sweep
        for lyr in MemoryLayer:
            layer_dir = mem_dir / lyr.value
            if layer_dir.is_dir():
                result.by_layer[lyr.value] = sum(1 for _ in layer_dir.glob("*.json"))

        # Run maintenance passes: dedup, compress, archive
        if not dry_run:
            try:
                self.dedup_memories()
            except Exception as exc:
                logger.error("Dedup pass failed: %s", exc)
            try:
                self.compress_memories()
            except Exception as exc:
                logger.error("Compress pass failed: %s", exc)
            try:
                self.archive_old_memories()
            except Exception as exc:
                logger.error("Archive pass failed: %s", exc)

        self._record_sweep(result)
        return result

    def score(self, entry: MemoryEntry) -> PromotionCandidate:
        """Score a single memory for promotion potential.

        Useful for inspecting why a particular memory would or
        wouldn't be promoted.

        Args:
            entry: The MemoryEntry to score.

        Returns:
            PromotionCandidate with score breakdown.
        """
        return self._evaluate(entry)

    def get_history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Read promotion history from the log.

        Args:
            limit: Maximum entries to return.

        Returns:
            List of promotion history dicts, newest first.
        """
        log_path = self._home / "memory" / "promotion-log.json"
        if not log_path.exists():
            return []
        try:
            data = json.loads(log_path.read_text(encoding="utf-8"))
            return data[-limit:]
        except (json.JSONDecodeError, Exception):
            return []

    # -------------------------------------------------------------------
    # Dedup / Compress / Archive
    # -------------------------------------------------------------------

    def dedup_memories(self) -> int:
        """Scan all memory tiers for duplicate and near-duplicate memories.

        Duplicates are detected by:
        - Exact title match (case-insensitive), using the raw JSON ``title``
          field or falling back to the first line of ``content``.
        - Near-duplicate: first 50 characters of the title match.

        When duplicates are found, the newest memory (by ``created_at``) is
        kept and the rest are moved to an ``archive/deduped/`` directory.

        Returns:
            Number of duplicate memories removed.
        """
        mem_dir = _memory_dir(self._home)
        removed = 0

        # Collect all memories across tiers with their raw titles
        entries_by_title: dict[str, list[tuple[Path, dict, MemoryEntry]]] = defaultdict(list)

        for lyr in MemoryLayer:
            layer_dir = mem_dir / lyr.value
            if not layer_dir.is_dir():
                continue
            for f in sorted(layer_dir.glob("*.json")):
                try:
                    raw = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                entry = _load_entry(f)
                if entry is None:
                    continue
                # Use raw title if present, otherwise first line of content
                title = raw.get("title", entry.content.split("\n", 1)[0])
                norm_title = title.strip().lower()
                entries_by_title[norm_title].append((f, raw, entry))

        # Phase 1: exact title duplicates
        deduped_ids: list[str] = []
        for title, group in entries_by_title.items():
            if len(group) <= 1:
                continue
            # Sort by created_at descending — keep newest
            group.sort(key=lambda g: g[2].created_at, reverse=True)
            keeper = group[0]
            for path, raw, entry in group[1:]:
                self._archive_deduped(path, entry)
                deduped_ids.append(entry.memory_id)
                removed += 1
                logger.info(
                    "Dedup: archived %s (dup of %s, title='%s')",
                    entry.memory_id, keeper[2].memory_id, title[:60],
                )

        # Phase 2: near-duplicates (first 50 chars of title match)
        # Re-collect surviving memories (exclude already-deduped)
        prefix_groups: dict[str, list[tuple[Path, dict, MemoryEntry]]] = defaultdict(list)
        for title, group in entries_by_title.items():
            for path, raw, entry in group:
                if entry.memory_id in deduped_ids:
                    continue
                if not path.exists():
                    continue
                prefix = title[:50]
                prefix_groups[prefix].append((path, raw, entry))

        for prefix, group in prefix_groups.items():
            if len(group) <= 1:
                continue
            group.sort(key=lambda g: g[2].created_at, reverse=True)
            keeper = group[0]
            for path, raw, entry in group[1:]:
                if entry.memory_id in deduped_ids:
                    continue
                self._archive_deduped(path, entry)
                deduped_ids.append(entry.memory_id)
                removed += 1
                logger.info(
                    "Dedup (near): archived %s (near-dup of %s, prefix='%s')",
                    entry.memory_id, keeper[2].memory_id, prefix[:50],
                )

        # Log dedup actions to promotion-log.json
        if removed > 0:
            self._record_dedup(removed, deduped_ids)

        return removed

    def compress_memories(self) -> int:
        """Compress older memories by truncating content.

        Rules:
        - Mid-term memories older than 7 days: truncate to first 500 chars + "..."
        - Long-term memories older than 30 days: keep only first 200 chars as summary
        - Memories tagged "seed", "core", or "identity" are never compressed.

        Returns:
            Number of memories compressed.
        """
        mem_dir = _memory_dir(self._home)
        now = datetime.now(timezone.utc)
        compressed = 0

        compress_rules = [
            (MemoryLayer.MID_TERM, timedelta(days=7), 500),
            (MemoryLayer.LONG_TERM, timedelta(days=30), 200),
        ]

        for lyr, age_threshold, max_chars in compress_rules:
            layer_dir = mem_dir / lyr.value
            if not layer_dir.is_dir():
                continue
            for f in sorted(layer_dir.glob("*.json")):
                entry = _load_entry(f)
                if entry is None:
                    continue

                # Skip protected memories
                if self._is_protected(entry):
                    continue

                age = now - entry.created_at
                if age < age_threshold:
                    continue

                # Already short enough — skip
                if len(entry.content) <= max_chars:
                    continue

                entry.content = entry.content[:max_chars] + "..."
                _save_entry(self._home, entry)
                compressed += 1
                logger.info(
                    "Compressed %s (%s, age=%dd, truncated to %d chars)",
                    entry.memory_id, lyr.value, age.days, max_chars,
                )

        return compressed

    def archive_old_memories(self) -> int:
        """Move memories older than 60 days to an archive directory.

        Scans all tiers and moves qualifying memories to
        ``~/.skcapstone/agents/<agent>/memory/archive/``.
        Memories tagged "seed", "core", or "identity" are never archived.

        Returns:
            Number of memories archived.
        """
        mem_dir = _memory_dir(self._home)
        archive_dir = mem_dir / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        threshold = timedelta(days=60)
        archived = 0

        for lyr in MemoryLayer:
            layer_dir = mem_dir / lyr.value
            if not layer_dir.is_dir():
                continue
            for f in sorted(layer_dir.glob("*.json")):
                entry = _load_entry(f)
                if entry is None:
                    continue

                if self._is_protected(entry):
                    continue

                age = now - entry.created_at
                if age < threshold:
                    continue

                dest = archive_dir / f.name
                shutil.move(str(f), str(dest))
                _remove_from_index(self._home, entry.memory_id)
                archived += 1
                logger.info(
                    "Archived %s (%s, age=%dd) -> archive/",
                    entry.memory_id, lyr.value, age.days,
                )

        return archived

    def _is_protected(self, entry: MemoryEntry) -> bool:
        """Check if a memory has protected tags (seed/core/identity).

        Args:
            entry: The MemoryEntry to check.

        Returns:
            True if the memory should not be compressed or archived.
        """
        return bool(set(t.lower() for t in entry.tags) & PROTECTED_TAGS)

    def _archive_deduped(self, path: Path, entry: MemoryEntry) -> None:
        """Move a deduplicated memory to the deduped archive.

        Args:
            path: Current file path.
            entry: The MemoryEntry being archived.
        """
        mem_dir = _memory_dir(self._home)
        dedup_dir = mem_dir / "archive" / "deduped"
        dedup_dir.mkdir(parents=True, exist_ok=True)
        dest = dedup_dir / path.name
        if path.exists():
            shutil.move(str(path), str(dest))
        _remove_from_index(self._home, entry.memory_id)

    def _record_dedup(self, count: int, deduped_ids: list[str]) -> None:
        """Append dedup results to the promotion log.

        Args:
            count: Number of duplicates removed.
            deduped_ids: List of memory IDs that were archived.
        """
        log_path = self._home / "memory" / "promotion-log.json"
        history: list[dict] = []
        if log_path.exists():
            try:
                history = json.loads(log_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception):
                history = []

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": "dedup",
            "removed": count,
            "deduped_ids": deduped_ids[-50:],  # cap list size
        }
        history.append(entry)

        if len(history) > 100:
            history = history[-100:]

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

    # -------------------------------------------------------------------
    # Scoring
    # -------------------------------------------------------------------

    def _evaluate(self, entry: MemoryEntry) -> PromotionCandidate:
        """Evaluate a memory entry for promotion.

        Computes a weighted score from multiple signals:
        - Access frequency: access_count normalized by age
        - Importance: raw importance score
        - Emotional intensity: presence of emotional tags
        - Age maturity: older important memories score higher
        - Tag richness: well-tagged memories are more organized

        Args:
            entry: The MemoryEntry to evaluate.

        Returns:
            PromotionCandidate with computed score.
        """
        t = self._thresholds

        access_score = self._score_access(entry)
        importance_score = entry.importance
        emotion_score = self._score_emotion(entry)
        age_score = self._score_age(entry)
        tag_score = self._score_tags(entry)

        weighted = (
            access_score * t.access_weight
            + importance_score * t.importance_weight
            + emotion_score * t.emotion_weight
            + age_score * t.age_weight
            + tag_score * t.tag_weight
        )

        # Clamp to [0, 1]
        score = max(0.0, min(1.0, weighted))

        target = self._target_layer(entry.layer)

        return PromotionCandidate(
            memory_id=entry.memory_id,
            current_layer=entry.layer.value,
            target_layer=target,
            score=round(score, 4),
            signals={
                "access": round(access_score, 4),
                "importance": round(importance_score, 4),
                "emotion": round(emotion_score, 4),
                "age": round(age_score, 4),
                "tags": round(tag_score, 4),
            },
        )

    def _score_access(self, entry: MemoryEntry) -> float:
        """Score based on access frequency.

        Higher access count relative to age = more valuable.
        """
        age = max(entry.age_hours, 1.0)
        # Normalize: 1 access per hour = score 1.0
        freq = entry.access_count / age
        return min(1.0, freq * 10)

    def _score_emotion(self, entry: MemoryEntry) -> float:
        """Score based on emotional content.

        Checks tags and content for emotional indicators.
        """
        tag_hits = sum(1 for t in entry.tags if t.lower() in EMOTIONAL_TAGS)
        content_hits = sum(
            1 for p in HIGH_VALUE_PATTERNS if p.search(entry.content)
        )
        # Normalize: 3+ hits = max score
        return min(1.0, (tag_hits + content_hits) / 3)

    def _score_age(self, entry: MemoryEntry) -> float:
        """Score based on age-importance interaction.

        Older memories with high importance score higher — they've
        proven their worth by persisting.
        """
        age = entry.age_hours
        if entry.layer == MemoryLayer.SHORT_TERM:
            # Short-term: promote after 24h if important
            if age > 24:
                return min(1.0, entry.importance * (age / 72))
            return 0.0
        # Mid-term: promote after 168h (1 week) if important
        if age > 168:
            return min(1.0, entry.importance * (age / 720))
        return 0.0

    def _score_tags(self, entry: MemoryEntry) -> float:
        """Score based on tag richness.

        Well-tagged memories indicate organized, valuable content.
        """
        n = len(entry.tags)
        if n == 0:
            return 0.0
        # 5+ tags = max score
        return min(1.0, n / 5)

    # -------------------------------------------------------------------
    # Promotion
    # -------------------------------------------------------------------

    def _promote(self, entry: MemoryEntry, old_path: Path) -> None:
        """Promote a memory to the next tier.

        Args:
            entry: The MemoryEntry to promote.
            old_path: Current file path (will be removed).
        """
        old_layer = entry.layer

        if entry.layer == MemoryLayer.SHORT_TERM:
            entry.layer = MemoryLayer.MID_TERM
        elif entry.layer == MemoryLayer.MID_TERM:
            entry.layer = MemoryLayer.LONG_TERM
        else:
            return

        if old_path.exists():
            old_path.unlink()

        _save_entry(self._home, entry)
        _update_index(self._home, entry)

        logger.info(
            "Promoted %s: %s -> %s",
            entry.memory_id, old_layer.value, entry.layer.value,
        )

    def _generate_summary(self, entry: MemoryEntry) -> str:
        """Generate a short summary for a promoted memory.

        Args:
            entry: The promoted MemoryEntry.

        Returns:
            A brief summary string.
        """
        content = entry.content
        if len(content) <= 80:
            return content
        return content[:77] + "..."

    def _target_layer(self, current: MemoryLayer) -> str:
        """Get the promotion target layer name."""
        if current == MemoryLayer.SHORT_TERM:
            return MemoryLayer.MID_TERM.value
        if current == MemoryLayer.MID_TERM:
            return MemoryLayer.LONG_TERM.value
        return current.value

    def _get_threshold(self, layer: MemoryLayer) -> float:
        """Get the promotion threshold for a layer."""
        if layer == MemoryLayer.SHORT_TERM:
            return self._thresholds.short_to_mid
        return self._thresholds.mid_to_long

    # -------------------------------------------------------------------
    # History
    # -------------------------------------------------------------------

    def _record_sweep(self, result: SweepResult) -> None:
        """Append sweep results to the promotion log."""
        log_path = self._home / "memory" / "promotion-log.json"
        history: list[dict] = []
        if log_path.exists():
            try:
                history = json.loads(log_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, Exception):
                history = []

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "scanned": result.scanned,
            "candidates": len(result.candidates),
            "promoted": len(result.promoted),
            "skipped": result.skipped,
            "dry_run": result.dry_run,
            "by_layer": result.by_layer,
            "promoted_ids": [c.memory_id for c in result.promoted],
        }
        history.append(entry)

        # Keep last 100 entries
        if len(history) > 100:
            history = history[-100:]

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps(history, indent=2), encoding="utf-8")
