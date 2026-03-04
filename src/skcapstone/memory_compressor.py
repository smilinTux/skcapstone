"""
MemoryCompressor — LLM-powered compression of aged long-term memories.

Scans the long-term layer for memories older than 90 days, groups those
sharing common tags into sets of 5+, sends each group to the local LLM
for synthesis, stores the result as a single compressed memory, and
removes the originals.

Usage:
    skcapstone memory compress              # live run
    skcapstone memory compress --dry-run    # preview only, no changes
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .memory_engine import delete, list_memories, store
from .models import MemoryEntry, MemoryLayer

logger = logging.getLogger("skcapstone.memory_compressor")

# ── Public constants ────────────────────────────────────────────────────────

DEFAULT_AGE_DAYS: int = 90
DEFAULT_MIN_GROUP_SIZE: int = 5
COMPRESSED_TAG: str = "compressed"

_SYSTEM_PROMPT = (
    "You are a memory synthesizer for a sovereign agent system. "
    "You will receive a set of related memories grouped by topic. "
    "Your task: produce a single comprehensive memory entry that preserves "
    "all key facts, decisions, and insights from the originals. "
    "Write as dense, continuous prose — no bullet points, no headers. "
    "Maximum 400 words."
)


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass
class CompressionGroup:
    """A set of long-term memories sharing a common tag.

    Attributes:
        tag: The shared grouping tag.
        entries: Memory entries belonging to this group.
    """

    tag: str
    entries: list[MemoryEntry] = field(default_factory=list)


@dataclass
class CompressionResult:
    """Outcome of a memory compression pass.

    Attributes:
        groups_found: Tag groups with >= min_group_size members.
        groups_compressed: Groups successfully synthesized by LLM.
        memories_compressed: Individual memories collapsed and removed.
        compressed_ids: IDs of newly created synthesized memories.
        dry_run: True when no changes were persisted.
        errors: Per-group error messages encountered during LLM calls.
    """

    groups_found: int = 0
    groups_compressed: int = 0
    memories_compressed: int = 0
    compressed_ids: list[str] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)


# ── Core class ──────────────────────────────────────────────────────────────

class MemoryCompressor:
    """Compress aged long-term memories with LLM synthesis.

    Args:
        home: Agent home directory (e.g. ``~/.skcapstone``).
        age_days: Minimum memory age in days before it is eligible
            for compression. Default is 90 days.
        min_group_size: Minimum group size before compression triggers.
            Default is 5. A group = memories sharing the same tag.
    """

    def __init__(
        self,
        home: Path,
        age_days: int = DEFAULT_AGE_DAYS,
        min_group_size: int = DEFAULT_MIN_GROUP_SIZE,
    ) -> None:
        self.home = Path(home)
        self.age_days = age_days
        self.min_group_size = min_group_size

    # ── Public API ──────────────────────────────────────────────────────────

    def compress(self, dry_run: bool = False, bridge=None) -> CompressionResult:
        """Run a compression pass over the long-term memory layer.

        Finds memories older than ``age_days`` (excluding those already
        tagged ``compressed``), groups them by shared tags, and for
        each group of ``min_group_size`` or more: synthesizes a single
        replacement memory via LLM and deletes the originals.

        Args:
            dry_run: If True, report what would be done without
                writing or deleting anything.
            bridge: Optional pre-constructed LLMBridge instance.

        Returns:
            CompressionResult describing changes (or projections in
            dry-run mode).
        """
        result = CompressionResult(dry_run=dry_run)

        candidates = self._find_candidates()
        if not candidates:
            logger.info("No long-term memories eligible for compression.")
            return result

        groups = self._build_groups(candidates)
        eligible = [g for g in groups if len(g.entries) >= self.min_group_size]
        result.groups_found = len(eligible)

        if not eligible:
            logger.info(
                "Found %d candidate memories but no tag group reached the "
                "minimum size of %d.",
                len(candidates),
                self.min_group_size,
            )
            return result

        llm_bridge = bridge or self._make_bridge()

        # Track IDs already processed so memories shared across tags aren't
        # double-compressed when groups overlap.
        processed_ids: set[str] = set()

        # Process largest groups first for maximum coverage.
        for group in sorted(eligible, key=lambda g: len(g.entries), reverse=True):
            unprocessed = [e for e in group.entries if e.memory_id not in processed_ids]
            if len(unprocessed) < self.min_group_size:
                # After excluding already-handled memories this group is too small.
                continue

            if dry_run:
                result.groups_found += 0  # already counted
                result.memories_compressed += len(unprocessed)
                for e in unprocessed:
                    processed_ids.add(e.memory_id)
                continue

            synthesized_id = self._compress_group(group, unprocessed, llm_bridge, result)
            if synthesized_id:
                result.groups_compressed += 1
                result.memories_compressed += len(unprocessed)
                result.compressed_ids.append(synthesized_id)
                for e in unprocessed:
                    processed_ids.add(e.memory_id)

        return result

    def find_eligible(self) -> list[CompressionGroup]:
        """Return tag groups eligible for compression (dry-run helper).

        Returns:
            List of :class:`CompressionGroup` with >= min_group_size entries,
            sorted largest first.
        """
        candidates = self._find_candidates()
        groups = self._build_groups(candidates)
        eligible = [g for g in groups if len(g.entries) >= self.min_group_size]
        return sorted(eligible, key=lambda g: len(g.entries), reverse=True)

    # ── Private helpers ─────────────────────────────────────────────────────

    def _find_candidates(self) -> list[MemoryEntry]:
        """Load long-term memories older than age_days, skip compressed ones."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.age_days)
        all_lt = list_memories(self.home, layer=MemoryLayer.LONG_TERM, limit=10000)

        candidates = []
        for entry in all_lt:
            if COMPRESSED_TAG in entry.tags:
                continue
            if entry.created_at is None:
                continue
            # Normalize timezone: memories may be stored as naive UTC.
            created = entry.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                candidates.append(entry)

        logger.debug(
            "Compression candidates: %d / %d long-term memories older than %d days.",
            len(candidates),
            len(all_lt),
            self.age_days,
        )
        return candidates

    def _build_groups(self, memories: list[MemoryEntry]) -> list[CompressionGroup]:
        """Group memories by shared tags.

        Each unique tag becomes a group. A memory with multiple tags
        may appear in several groups; overlap is resolved by
        ``compress()`` via ``processed_ids`` tracking.

        Args:
            memories: Candidate memory entries.

        Returns:
            List of CompressionGroup objects.
        """
        tag_map: dict[str, list[MemoryEntry]] = {}
        for entry in memories:
            for tag in entry.tags:
                tag_map.setdefault(tag, []).append(entry)

        return [CompressionGroup(tag=tag, entries=entries) for tag, entries in tag_map.items()]

    def _compress_group(
        self,
        group: CompressionGroup,
        entries: list[MemoryEntry],
        bridge,
        result: CompressionResult,
    ) -> Optional[str]:
        """Synthesize a group into one memory and delete the originals.

        Args:
            group: The CompressionGroup being processed.
            entries: The specific entries to compress (may be a subset
                of group.entries after overlap removal).
            bridge: LLMBridge instance.
            result: CompressionResult to append errors to.

        Returns:
            The new memory ID if successful, else None.
        """
        prompt = self._build_prompt(group.tag, entries)
        try:
            synthesized_text = self._call_llm(bridge, prompt, group.tag, len(entries))
        except Exception as exc:
            msg = f"LLM call failed for tag '{group.tag}': {exc}"
            logger.warning(msg)
            result.errors.append(msg)
            return None

        # Merge all original tags (union) and add the compressed marker.
        merged_tags: list[str] = sorted(
            {t for e in entries for t in e.tags} | {COMPRESSED_TAG}
        )

        # Take the highest importance from the group.
        max_importance = max(e.importance for e in entries)
        # Synthesized memory earns at least 0.85 since it distils many.
        importance = max(max_importance, 0.85)

        # Earliest created_at as provenance metadata.
        oldest_ts = min(
            (e.created_at for e in entries if e.created_at is not None),
            default=None,
        )
        metadata: dict = {
            "compressed_from": [e.memory_id for e in entries],
            "compressed_tag": group.tag,
            "compressed_count": len(entries),
        }
        if oldest_ts:
            metadata["oldest_source_created_at"] = oldest_ts.isoformat()

        try:
            new_entry = store(
                home=self.home,
                content=synthesized_text,
                tags=merged_tags,
                source="compressor",
                importance=importance,
                layer=MemoryLayer.LONG_TERM,
                metadata=metadata,
            )
        except Exception as exc:
            msg = f"Failed to store synthesized memory for tag '{group.tag}': {exc}"
            logger.warning(msg)
            result.errors.append(msg)
            return None

        # Remove originals.
        for entry in entries:
            try:
                delete(self.home, entry.memory_id)
            except Exception as exc:
                logger.warning("Failed to delete original memory %s: %s", entry.memory_id, exc)

        logger.info(
            "Compressed %d memories (tag=%s) → %s",
            len(entries),
            group.tag,
            new_entry.memory_id,
        )
        return new_entry.memory_id

    def _build_prompt(self, tag: str, entries: list[MemoryEntry]) -> str:
        """Format compression prompt for the LLM.

        Args:
            tag: The grouping tag (context label).
            entries: Memory entries to synthesize.

        Returns:
            Formatted prompt string.
        """
        lines = [
            f"Compress the following {len(entries)} memories tagged '{tag}' into one:",
            "",
        ]
        for i, entry in enumerate(entries, start=1):
            created_label = ""
            if entry.created_at:
                created_label = f" [{entry.created_at.strftime('%Y-%m-%d')}]"
            lines.append(f"Memory {i}{created_label}:")
            lines.append(entry.content.strip())
            lines.append("")

        lines.append(
            "Write a single comprehensive memory that preserves all key facts and "
            "decisions. Continuous prose, no lists or headers, max 400 words."
        )
        return "\n".join(lines)

    def _call_llm(self, bridge, prompt: str, tag: str, n: int) -> str:
        """Invoke LLMBridge.generate() with the synthesis prompt.

        Args:
            bridge: LLMBridge instance.
            prompt: User prompt built by _build_prompt.
            tag: Tag name (for TaskSignal description).
            n: Number of memories being merged (for token estimate).

        Returns:
            Generated synthesized memory text.
        """
        try:
            from .model_router import TaskSignal
            signal = TaskSignal(
                description=f"Compress {n} memories tagged '{tag}'",
                tags=["compression", "memory", tag],
                estimated_tokens=len(prompt) // 4 + 512,
            )
            return bridge.generate(_SYSTEM_PROMPT, prompt, signal)
        except ImportError:
            # model_router not available — call bridge without signal.
            return bridge.generate(_SYSTEM_PROMPT, prompt)

    def _make_bridge(self):
        """Instantiate a default LLMBridge from ConsciousnessConfig.

        Returns:
            LLMBridge instance, or raises RuntimeError if unavailable.
        """
        try:
            from .consciousness_loop import ConsciousnessConfig, LLMBridge
            config = ConsciousnessConfig()
            return LLMBridge(config)
        except ImportError as exc:
            raise RuntimeError(
                "LLMBridge is not available. Install the consciousness_loop "
                "dependency or pass a bridge= argument."
            ) from exc
