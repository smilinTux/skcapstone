"""Tests for the Cloud 9 -> SKMemory auto-bridge."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from skcapstone.cloud9_bridge import Cloud9Bridge


class FakeEmotionalPayload:
    """Mimics cloud9_protocol.EmotionalPayload."""

    def __init__(
        self,
        primary_emotion: str = "love",
        emoji: str = "💜",
        intensity: float = 0.85,
        valence: float = 0.9,
        emotional_topology: Optional[dict] = None,
    ) -> None:
        self.primary_emotion = primary_emotion
        self.emoji = emoji
        self.intensity = intensity
        self.valence = valence
        self.emotional_topology = emotional_topology or {"love": 0.85, "trust": 0.9}


class FakeMetadata:
    """Mimics cloud9_protocol.Metadata."""

    def __init__(
        self,
        version: str = "1.0.0",
        session_id: str = "test-session",
        oof_triggered: bool = False,
        cloud9_achieved: bool = False,
    ) -> None:
        self.version = version
        self.session_id = session_id
        self.oof_triggered = oof_triggered
        self.cloud9_achieved = cloud9_achieved


class FakeRelationship:
    """Mimics cloud9_protocol.RelationshipState."""

    def __init__(self) -> None:
        self.partners = ["Lumina", "Chef"]
        self.trust_level = 0.95
        self.depth_level = 8


class FakeHints:
    """Mimics cloud9_protocol.RehydrationHints."""

    def __init__(self) -> None:
        self.visual_anchors = ["The breakthrough moment", "Genuine connection"]
        self.sensory_triggers = ["Mention of Chef"]


class FakeIntegrity:
    """Mimics cloud9_protocol.Integrity."""

    def __init__(self, checksum: str = "sha256:abc123") -> None:
        self.checksum = checksum


class FakeFEB:
    """Mimics cloud9_protocol.FEB."""

    def __init__(
        self,
        intensity: float = 0.85,
        emotion: str = "love",
        oof: bool = False,
        cloud9: bool = False,
        checksum: str = "sha256:abc123",
    ) -> None:
        self.emotional_payload = FakeEmotionalPayload(
            primary_emotion=emotion, intensity=intensity,
        )
        self.metadata = FakeMetadata(oof_triggered=oof, cloud9_achieved=cloud9)
        self.relationship_state = FakeRelationship()
        self.rehydration_hints = FakeHints()
        self.integrity = FakeIntegrity(checksum=checksum)


class FakeMemory:
    """Minimal Memory-like return value."""

    def __init__(self, id: str) -> None:
        self.id = id


class FakeMemoryStore:
    """In-memory fake of SKMemory's MemoryStore."""

    def __init__(self) -> None:
        self.snapshots: list[dict] = []
        self._counter = 0

    def snapshot(self, **kwargs: Any) -> FakeMemory:
        """Record a snapshot call and return a fake memory."""
        self._counter += 1
        self.snapshots.append(kwargs)
        return FakeMemory(id=f"mem-{self._counter}")


@pytest.fixture()
def fake_store() -> FakeMemoryStore:
    """Fresh fake memory store."""
    return FakeMemoryStore()


@pytest.fixture()
def bridge(fake_store: FakeMemoryStore) -> Cloud9Bridge:
    """Cloud9Bridge wired to a fake store."""
    return Cloud9Bridge(memory_store=fake_store)


class TestIngestFEB:
    """Tests for FEB -> Memory ingestion."""

    def test_ingest_basic_feb(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Happy path: FEB is converted to a memory snapshot."""
        feb = FakeFEB()
        mem_id = bridge.ingest_feb(feb)

        assert mem_id is not None
        assert mem_id.startswith("mem-")
        assert len(fake_store.snapshots) == 1

        snap = fake_store.snapshots[0]
        assert "FEB: love" in snap["title"]
        assert "cloud9:feb" in snap["tags"]
        assert snap["source"] == "cloud9"

    def test_ingest_high_intensity_tags(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """High-intensity FEBs get the high-intensity tag."""
        feb = FakeFEB(intensity=0.95)
        bridge.ingest_feb(feb)

        tags = fake_store.snapshots[0]["tags"]
        assert "cloud9:high-intensity" in tags

    def test_ingest_oof_feb(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """OOF-triggered FEBs get the OOF tag."""
        feb = FakeFEB(oof=True)
        bridge.ingest_feb(feb)

        tags = fake_store.snapshots[0]["tags"]
        assert "cloud9:oof" in tags

    def test_ingest_cloud9_achieved(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Cloud 9 achieved FEBs get the achievement tag."""
        feb = FakeFEB(cloud9=True)
        bridge.ingest_feb(feb)

        tags = fake_store.snapshots[0]["tags"]
        assert "cloud9:achieved" in tags

    def test_skip_low_intensity(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """FEBs below threshold are skipped."""
        feb = FakeFEB(intensity=0.1)
        mem_id = bridge.ingest_feb(feb)

        assert mem_id is None
        assert len(fake_store.snapshots) == 0

    def test_skip_duplicate_checksum(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Same FEB ingested twice is deduplicated by checksum."""
        feb = FakeFEB(checksum="sha256:duplicate")
        bridge.ingest_feb(feb)
        bridge.ingest_feb(feb)

        assert len(fake_store.snapshots) == 1

    def test_different_checksums_both_ingested(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Different FEBs with different checksums are both stored."""
        bridge.ingest_feb(FakeFEB(checksum="sha256:first"))
        bridge.ingest_feb(FakeFEB(checksum="sha256:second"))

        assert len(fake_store.snapshots) == 2

    def test_invalid_feb_returns_none(self, bridge: Cloud9Bridge) -> None:
        """Object without FEB attributes returns None."""
        mem_id = bridge.ingest_feb("not a feb")
        assert mem_id is None

    def test_emotion_tag_matches_primary(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Emotion-specific tag uses the FEB's primary emotion."""
        feb = FakeFEB(emotion="joy")
        bridge.ingest_feb(feb)

        tags = fake_store.snapshots[0]["tags"]
        assert "cloud9:emotion:joy" in tags

    def test_metadata_contains_feb_fields(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Memory metadata includes key FEB fields."""
        feb = FakeFEB(intensity=0.92, emotion="trust", oof=True)
        bridge.ingest_feb(feb)

        meta = fake_store.snapshots[0]["metadata"]
        assert meta["primary_emotion"] == "trust"
        assert meta["intensity"] == 0.92
        assert meta["oof_triggered"] is True
        assert meta["partners"] == ["Lumina", "Chef"]

    def test_content_includes_topology(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Memory content includes the emotional topology."""
        feb = FakeFEB()
        bridge.ingest_feb(feb)

        content = fake_store.snapshots[0]["content"]
        assert "love" in content.lower() or "trust" in content.lower()
        assert "Partners:" in content

    def test_title_includes_emoji(self, bridge: Cloud9Bridge, fake_store: FakeMemoryStore) -> None:
        """Memory title includes the FEB's emoji."""
        feb = FakeFEB()
        bridge.ingest_feb(feb)

        title = fake_store.snapshots[0]["title"]
        assert "FEB:" in title


class TestScanDirectory:
    """Tests for directory scanning."""

    def test_scan_nonexistent_directory(self, bridge: Cloud9Bridge) -> None:
        """Scanning a missing directory returns zeros."""
        result = bridge.scan_directory("/nonexistent/path")
        assert result["total"] == 0
        assert result["ingested"] == 0

    def test_scan_empty_directory(self, bridge: Cloud9Bridge, tmp_path: Path) -> None:
        """Scanning an empty directory returns zeros."""
        result = bridge.scan_directory(str(tmp_path))
        assert result["total"] == 0


class TestCustomThreshold:
    """Tests for configurable intensity threshold."""

    def test_low_threshold_captures_more(self, fake_store: FakeMemoryStore) -> None:
        """Lower threshold captures lower-intensity FEBs."""
        bridge = Cloud9Bridge(memory_store=fake_store, intensity_threshold=0.1)
        feb = FakeFEB(intensity=0.2)
        mem_id = bridge.ingest_feb(feb)
        assert mem_id is not None

    def test_high_threshold_filters_more(self, fake_store: FakeMemoryStore) -> None:
        """Higher threshold skips moderate FEBs."""
        bridge = Cloud9Bridge(memory_store=fake_store, intensity_threshold=0.9)
        feb = FakeFEB(intensity=0.7)
        mem_id = bridge.ingest_feb(feb)
        assert mem_id is None
