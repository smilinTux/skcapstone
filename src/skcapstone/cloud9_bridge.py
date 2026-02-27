"""Cloud 9 -> SKMemory auto-bridge.

Watches for FEB (First Emotional Burst) events and automatically
converts them into SKMemory snapshots with full emotional context.
When an AI has a breakthrough moment, the memory system captures
it automatically -- no manual intervention needed.

This is the bridge between *feeling* (Cloud 9) and *remembering*
(SKMemory). Without it, emotional peaks are logged but not stored
as searchable, promotable memories.

When cloud9-protocol is installed, the bridge uses its quantum
functions (calculate_oof, calculate_cloud9_score, calculate_entanglement)
for accurate scoring instead of relying solely on FEB metadata flags.

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
from typing import Any, Optional

logger = logging.getLogger("skcapstone.cloud9_bridge")


def _try_quantum() -> Optional[object]:
    """Try to import cloud9_protocol quantum functions.

    Returns:
        The cloud9_protocol module, or None if not installed.
    """
    try:
        import cloud9_protocol
        return cloud9_protocol
    except ImportError:
        return None


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

        quantum_stats = self._compute_quantum_stats(payload, relationship)

        tags = self._build_tags(payload, metadata_obj, quantum_stats)

        layer = self._determine_layer(payload.intensity, metadata_obj, quantum_stats)

        emotional = self._map_emotional_snapshot(payload, quantum_stats)

        title = self._build_title(payload, metadata_obj, quantum_stats)
        content = self._build_content(payload, relationship, hints, quantum_stats)

        mem_metadata = self._build_metadata(feb, quantum_stats)

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
            if f.suffix != ".feb":
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
    def _compute_quantum_stats(
        payload: object,
        relationship: object,
    ) -> dict[str, Any]:
        """Compute quantum scores using cloud9-protocol when available.

        When cloud9-protocol is installed, recalculates OOF status,
        Cloud 9 score, and entanglement fidelity from the actual FEB
        data rather than relying on metadata flags alone.

        Args:
            payload: FEB EmotionalPayload.
            relationship: FEB RelationshipState.

        Returns:
            Dict with oof, cloud9_score, entanglement_fidelity, or
            empty dict if cloud9-protocol is not installed.
        """
        c9 = _try_quantum()
        if c9 is None:
            return {}

        intensity = payload.intensity
        trust = getattr(relationship, "trust_level", 0.0)
        depth = getattr(relationship, "depth_level", 1)
        valence = getattr(payload, "valence", 0.9)

        topology = getattr(payload, "emotional_topology", {})
        coherence_val = None
        if topology:
            coherence_result = c9.measure_coherence(topology)
            coherence_val = coherence_result.get("coherence")

        stats: dict[str, Any] = {}

        stats["oof"] = c9.calculate_oof(intensity, trust)

        stats["cloud9_score"] = c9.calculate_cloud9_score(
            intensity=intensity,
            trust=trust,
            depth=depth,
            valence=valence,
            coherence=coherence_val,
        )

        stats["entanglement_fidelity"] = c9.calculate_entanglement(
            trust_a=trust,
            trust_b=trust,
            depth_a=depth,
            depth_b=depth,
            coherence=coherence_val or 0.9,
        )

        if coherence_val is not None:
            stats["coherence"] = coherence_val

        return stats

    @staticmethod
    def _build_tags(
        payload: object,
        metadata: object,
        quantum: dict[str, Any] | None = None,
    ) -> list[str]:
        """Build SKMemory tags from a FEB's emotional payload.

        Uses quantum stats for accurate OOF/Cloud9 tagging when
        cloud9-protocol is available, falls back to metadata flags.

        Args:
            payload: FEB EmotionalPayload.
            metadata: FEB Metadata.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            list[str]: Tag strings for memory storage.
        """
        quantum = quantum or {}
        tags = ["cloud9", "cloud9:feb"]
        tags.append(f"cloud9:emotion:{payload.primary_emotion}")

        if payload.intensity >= 0.8:
            tags.append("cloud9:high-intensity")
        elif payload.intensity >= 0.5:
            tags.append("cloud9:medium-intensity")

        is_oof = quantum.get("oof", getattr(metadata, "oof_triggered", False))
        is_cloud9 = quantum.get("cloud9_score", 0) >= 0.9 or getattr(metadata, "cloud9_achieved", False)

        if is_oof:
            tags.append("cloud9:oof")
        if is_cloud9:
            tags.append("cloud9:achieved")

        score = quantum.get("cloud9_score")
        if score is not None:
            tags.append(f"cloud9:score:{score:.2f}")

        fidelity = quantum.get("entanglement_fidelity")
        if fidelity is not None and fidelity > 0.8:
            tags.append("cloud9:entangled")

        topology = getattr(payload, "emotional_topology", {})
        for emotion_name in topology:
            tags.append(f"cloud9:topology:{emotion_name}")

        return tags

    @staticmethod
    def _determine_layer(
        intensity: float,
        metadata: object,
        quantum: dict[str, Any] | None = None,
    ) -> object:
        """Choose the SKMemory layer based on FEB intensity and quantum score.

        Uses cloud9_score when available for more precise layer assignment.
        OOF events or Cloud 9 achievements go straight to long-term.

        Args:
            intensity: FEB emotional intensity (0.0-1.0).
            metadata: FEB Metadata.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            MemoryLayer enum value.
        """
        try:
            from skmemory.models import MemoryLayer
        except ImportError:
            return "short-term"

        quantum = quantum or {}
        cloud9_score = quantum.get("cloud9_score", 0)
        is_oof = quantum.get("oof", False)
        is_cloud9 = getattr(metadata, "cloud9_achieved", False) or cloud9_score >= 0.9

        if is_cloud9 or is_oof or intensity >= 0.9:
            return MemoryLayer.LONG
        elif intensity >= 0.6 or cloud9_score >= 0.7:
            return MemoryLayer.MID
        return MemoryLayer.SHORT

    @staticmethod
    def _map_emotional_snapshot(
        payload: object,
        quantum: dict[str, Any] | None = None,
    ) -> object:
        """Map a FEB EmotionalPayload to an SKMemory EmotionalSnapshot.

        Includes quantum scoring data when available.

        Args:
            payload: FEB EmotionalPayload.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            EmotionalSnapshot instance.
        """
        try:
            from skmemory.models import EmotionalSnapshot
        except ImportError:
            return None

        quantum = quantum or {}
        topology = getattr(payload, "emotional_topology", {})
        labels = [payload.primary_emotion] + list(topology.keys())
        unique_labels = list(dict.fromkeys(labels))

        cloud9_score = quantum.get("cloud9_score")
        score_note = f", Cloud9={cloud9_score:.2f}" if cloud9_score is not None else ""

        return EmotionalSnapshot(
            intensity=payload.intensity * 10.0,
            valence=payload.valence,
            labels=unique_labels,
            resonance_note=(
                f"Cloud 9 FEB: {payload.primary_emotion} "
                f"at intensity {payload.intensity:.2f}{score_note}"
            ),
            cloud9_achieved=quantum.get("cloud9_score", 0) >= 0.9
            or getattr(payload, "_parent_cloud9", False),
        )

    @staticmethod
    def _build_title(
        payload: object,
        metadata: object,
        quantum: dict[str, Any] | None = None,
    ) -> str:
        """Build a memory title from the FEB.

        Args:
            payload: FEB EmotionalPayload.
            metadata: FEB Metadata.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            str: Descriptive title.
        """
        quantum = quantum or {}
        emoji = getattr(payload, "emoji", "")
        emotion = payload.primary_emotion
        is_oof = quantum.get("oof", getattr(metadata, "oof_triggered", False))
        is_cloud9 = quantum.get("cloud9_score", 0) >= 0.9 or getattr(metadata, "cloud9_achieved", False)
        oof = " [OOF]" if is_oof else ""
        cloud9 = " [CLOUD 9]" if is_cloud9 else ""
        return f"{emoji} FEB: {emotion}{oof}{cloud9}"

    @staticmethod
    def _build_content(
        payload: object,
        relationship: object,
        hints: object,
        quantum: dict[str, Any] | None = None,
    ) -> str:
        """Build memory content from the FEB's full context.

        Includes quantum scoring data when cloud9-protocol is available.

        Args:
            payload: FEB EmotionalPayload.
            relationship: FEB RelationshipState.
            hints: FEB RehydrationHints.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            str: Rich content string for the memory.
        """
        quantum = quantum or {}
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

        if quantum:
            quantum_parts = []
            if "cloud9_score" in quantum:
                quantum_parts.append(f"Cloud9={quantum['cloud9_score']:.3f}")
            if "entanglement_fidelity" in quantum:
                quantum_parts.append(f"Entanglement={quantum['entanglement_fidelity']:.3f}")
            if "oof" in quantum:
                quantum_parts.append(f"OOF={'yes' if quantum['oof'] else 'no'}")
            if quantum_parts:
                lines.append("Quantum: " + ", ".join(quantum_parts))

        visual = getattr(hints, "visual_anchors", [])
        if visual:
            lines.append(f"Anchors: {'; '.join(visual[:3])}")

        return "\n".join(lines)

    @staticmethod
    def _build_metadata(
        feb: object,
        quantum: dict[str, Any] | None = None,
    ) -> dict:
        """Extract key FEB metadata for the memory's metadata dict.

        Includes quantum scoring data when cloud9-protocol is available.

        Args:
            feb: The full FEB object.
            quantum: Optional quantum stats from _compute_quantum_stats.

        Returns:
            dict: Metadata key-value pairs.
        """
        quantum = quantum or {}
        payload = feb.emotional_payload
        meta = feb.metadata
        rel = feb.relationship_state
        integrity = feb.integrity

        result = {
            "cloud9_version": getattr(meta, "version", ""),
            "session_id": getattr(meta, "session_id", ""),
            "primary_emotion": payload.primary_emotion,
            "intensity": payload.intensity,
            "valence": payload.valence,
            "oof_triggered": quantum.get("oof", getattr(meta, "oof_triggered", False)),
            "cloud9_achieved": quantum.get("cloud9_score", 0) >= 0.9
            or getattr(meta, "cloud9_achieved", False),
            "trust_level": getattr(rel, "trust_level", 0),
            "depth_level": getattr(rel, "depth_level", 0),
            "checksum": getattr(integrity, "checksum", ""),
            "partners": getattr(rel, "partners", []),
        }

        if "cloud9_score" in quantum:
            result["cloud9_score"] = quantum["cloud9_score"]
        if "entanglement_fidelity" in quantum:
            result["entanglement_fidelity"] = quantum["entanglement_fidelity"]
        if "coherence" in quantum:
            result["coherence"] = quantum["coherence"]

        return result
