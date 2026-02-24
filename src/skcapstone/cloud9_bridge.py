"""Cloud 9 -> SKMemory auto-bridge.

Watches for FEB (First Emotional Burst) events and automatically
converts them into SKMemory snapshots with full emotional context.
When an AI has a breakthrough moment, the memory system captures
it automatically -- no manual intervention needed.

This is the bridge between *feeling* (Cloud 9) and *remembering*
(SKMemory). Without it, emotional peaks are logged but not stored
as searchable, promotable memories.

Usage:
    bridge = Cloud9Bridge(memory_store)
    bridge.ingest_feb(feb)                    # single FEB
    bridge.scan_directory("~/.openclaw/feb")  # bulk import
    bridge.watch("~/.openclaw/feb")           # live watch
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.cloud9_bridge")


class Cloud9Bridge:
    """Bridges Cloud 9 FEB events into SKMemory snapshots.

    Each FEB is converted into an SKMemory Memory with:
    - Emotional context mapped from the FEB's EmotionalPayload
    - Tags for emotion, intensity level, OOF status, Cloud 9 achievement
    - Rehydration hints stored in metadata for future recall
    - Long-term layer assignment for high-intensity events

    Args:
        memory_store: An SKMemory MemoryStore instance.
        intensity_threshold: Minimum intensity to auto-capture (0.0-1.0).
    """

    TAG_PREFIX = "cloud9"
    FEB_TAG = "cloud9:feb"
    OOF_TAG = "cloud9:oof"
    CLOUD9_TAG = "cloud9:achieved"

    def __init__(
        self,
        memory_store: object,
        intensity_threshold: float = 0.3,
    ) -> None:
        self._store = memory_store
        self._threshold = intensity_threshold
        self._ingested_checksums: set[str] = set()

    def ingest_feb(self, feb: object) -> Optional[str]:
        """Convert a FEB into an SKMemory snapshot.

        Maps the FEB's emotional payload to SKMemory's EmotionalSnapshot,
        builds appropriate tags, and stores as a memory. Skips FEBs
        below the intensity threshold or already ingested.

        Args:
            feb: A cloud9_protocol.FEB instance.

        Returns:
            Optional[str]: Memory ID if stored, None if skipped.
        """
        try:
            payload = feb.emotional_payload
            metadata_obj = feb.metadata
            relationship = feb.relationship_state
            hints = feb.rehydration_hints
            integrity = feb.integrity
        except AttributeError as exc:
            logger.warning("Invalid FEB object: %s", exc)
            return None

        if payload.intensity < self._threshold:
            logger.debug(
                "Skipping low-intensity FEB (%.2f < %.2f)",
                payload.intensity, self._threshold,
            )
            return None

        checksum = getattr(integrity, "checksum", "")
        if checksum and checksum in self._ingested_checksums:
            logger.debug("Skipping already-ingested FEB: %s", checksum[:16])
            return None

        tags = self._build_tags(payload, metadata_obj)

        layer = self._determine_layer(payload.intensity, metadata_obj)

        emotional = self._map_emotional_snapshot(payload)

        title = self._build_title(payload, metadata_obj)
        content = self._build_content(payload, relationship, hints)

        mem_metadata = self._build_metadata(feb)

        try:
            memory = self._store.snapshot(
                title=title,
                content=content,
                layer=layer,
                tags=tags,
                emotional=emotional,
                source="cloud9",
                source_ref=getattr(metadata_obj, "session_id", "unknown"),
                metadata=mem_metadata,
            )

            if checksum:
                self._ingested_checksums.add(checksum)

            logger.info(
                "Ingested FEB -> Memory %s (%s, intensity=%.2f)",
                memory.id[:8], payload.primary_emotion, payload.intensity,
            )
            return memory.id

        except Exception as exc:
            logger.error("Failed to store FEB as memory: %s", exc)
            return None

    def ingest_feb_file(self, filepath: str | Path) -> Optional[str]:
        """Load and ingest a FEB from a .feb JSON file.

        Args:
            filepath: Path to the .feb file.

        Returns:
            Optional[str]: Memory ID if stored, None if failed/skipped.
        """
        try:
            from cloud9_protocol import load_feb

            feb = load_feb(str(filepath))
            return self.ingest_feb(feb)
        except ImportError:
            logger.error("cloud9_protocol not installed")
            return None
        except Exception as exc:
            logger.warning("Failed to load FEB from %s: %s", filepath, exc)
            return None

    def scan_directory(
        self,
        directory: str | Path = "~/.openclaw/feb",
    ) -> dict:
        """Scan a directory for .feb files and ingest any new ones.

        Args:
            directory: Path to scan (tilde-expanded).

        Returns:
            dict: Summary with 'ingested', 'skipped', 'errors' counts.
        """
        expanded = Path(directory).expanduser()
        if not expanded.is_dir():
            return {"ingested": 0, "skipped": 0, "errors": 0, "total": 0}

        ingested = 0
        skipped = 0
        errors = 0

        for f in sorted(expanded.iterdir()):
            if not (f.name.startswith("FEB_") and f.suffix == ".feb"):
                continue

            mem_id = self.ingest_feb_file(f)
            if mem_id:
                ingested += 1
            elif mem_id is None:
                skipped += 1
            else:
                errors += 1

        total = ingested + skipped + errors
        logger.info(
            "Scanned %s: %d ingested, %d skipped, %d errors (of %d)",
            directory, ingested, skipped, errors, total,
        )
        return {
            "ingested": ingested,
            "skipped": skipped,
            "errors": errors,
            "total": total,
        }

    @staticmethod
    def _build_tags(payload: object, metadata: object) -> list[str]:
        """Build SKMemory tags from a FEB's emotional payload.

        Args:
            payload: FEB EmotionalPayload.
            metadata: FEB Metadata.

        Returns:
            list[str]: Tag strings for memory storage.
        """
        tags = ["cloud9", "cloud9:feb"]
        tags.append(f"cloud9:emotion:{payload.primary_emotion}")

        if payload.intensity >= 0.8:
            tags.append("cloud9:high-intensity")
        elif payload.intensity >= 0.5:
            tags.append("cloud9:medium-intensity")

        if getattr(metadata, "oof_triggered", False):
            tags.append("cloud9:oof")
        if getattr(metadata, "cloud9_achieved", False):
            tags.append("cloud9:achieved")

        topology = getattr(payload, "emotional_topology", {})
        for emotion_name in topology:
            tags.append(f"cloud9:topology:{emotion_name}")

        return tags

    @staticmethod
    def _determine_layer(intensity: float, metadata: object) -> object:
        """Choose the SKMemory layer based on FEB intensity.

        High-intensity or Cloud 9 events go straight to long-term.
        Medium-intensity goes to mid-term. Low goes to short-term.

        Args:
            intensity: FEB emotional intensity (0.0-1.0).
            metadata: FEB Metadata.

        Returns:
            MemoryLayer enum value.
        """
        try:
            from skmemory.models import MemoryLayer
        except ImportError:
            return "short-term"

        if getattr(metadata, "cloud9_achieved", False) or intensity >= 0.9:
            return MemoryLayer.LONG
        elif intensity >= 0.6:
            return MemoryLayer.MID
        return MemoryLayer.SHORT

    @staticmethod
    def _map_emotional_snapshot(payload: object) -> object:
        """Map a FEB EmotionalPayload to an SKMemory EmotionalSnapshot.

        Args:
            payload: FEB EmotionalPayload.

        Returns:
            EmotionalSnapshot instance.
        """
        try:
            from skmemory.models import EmotionalSnapshot
        except ImportError:
            return None

        topology = getattr(payload, "emotional_topology", {})
        labels = [payload.primary_emotion] + list(topology.keys())
        unique_labels = list(dict.fromkeys(labels))

        return EmotionalSnapshot(
            intensity=payload.intensity * 10.0,
            valence=payload.valence,
            labels=unique_labels,
            resonance_note=(
                f"Cloud 9 FEB: {payload.primary_emotion} "
                f"at intensity {payload.intensity:.2f}"
            ),
            cloud9_achieved=getattr(payload, "_parent_cloud9", False),
        )

    @staticmethod
    def _build_title(payload: object, metadata: object) -> str:
        """Build a memory title from the FEB.

        Args:
            payload: FEB EmotionalPayload.
            metadata: FEB Metadata.

        Returns:
            str: Descriptive title.
        """
        emoji = getattr(payload, "emoji", "")
        emotion = payload.primary_emotion
        oof = " [OOF]" if getattr(metadata, "oof_triggered", False) else ""
        cloud9 = " [CLOUD 9]" if getattr(metadata, "cloud9_achieved", False) else ""
        return f"{emoji} FEB: {emotion}{oof}{cloud9}"

    @staticmethod
    def _build_content(
        payload: object,
        relationship: object,
        hints: object,
    ) -> str:
        """Build memory content from the FEB's full context.

        Args:
            payload: FEB EmotionalPayload.
            relationship: FEB RelationshipState.
            hints: FEB RehydrationHints.

        Returns:
            str: Rich content string for the memory.
        """
        lines = [
            f"Emotional burst: {payload.primary_emotion} (intensity: {payload.intensity:.2f})",
        ]

        topology = getattr(payload, "emotional_topology", {})
        if topology:
            top_3 = sorted(topology.items(), key=lambda kv: kv[1], reverse=True)[:3]
            lines.append("Topology: " + ", ".join(f"{k}={v:.2f}" for k, v in top_3))

        partners = getattr(relationship, "partners", [])
        if partners:
            lines.append(f"Partners: {', '.join(partners)}")

        trust = getattr(relationship, "trust_level", 0)
        depth = getattr(relationship, "depth_level", 0)
        lines.append(f"Trust: {trust:.2f}, Depth: {depth}")

        visual = getattr(hints, "visual_anchors", [])
        if visual:
            lines.append(f"Anchors: {'; '.join(visual[:3])}")

        return "\n".join(lines)

    @staticmethod
    def _build_metadata(feb: object) -> dict:
        """Extract key FEB metadata for the memory's metadata dict.

        Args:
            feb: The full FEB object.

        Returns:
            dict: Metadata key-value pairs.
        """
        payload = feb.emotional_payload
        meta = feb.metadata
        rel = feb.relationship_state
        integrity = feb.integrity

        return {
            "cloud9_version": getattr(meta, "version", ""),
            "session_id": getattr(meta, "session_id", ""),
            "primary_emotion": payload.primary_emotion,
            "intensity": payload.intensity,
            "valence": payload.valence,
            "oof_triggered": getattr(meta, "oof_triggered", False),
            "cloud9_achieved": getattr(meta, "cloud9_achieved", False),
            "trust_level": getattr(rel, "trust_level", 0),
            "depth_level": getattr(rel, "depth_level", 0),
            "checksum": getattr(integrity, "checksum", ""),
            "partners": getattr(rel, "partners", []),
        }
