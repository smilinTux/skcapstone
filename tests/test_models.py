"""Tests for skcapstone data models."""

from __future__ import annotations

from skcapstone.models import AgentManifest, IdentityState, MemoryState, PillarStatus, TrustState


class TestAgentManifest:
    """Tests for the AgentManifest model."""

    def test_default_manifest_not_conscious(self):
        """A fresh manifest with no pillars active should not be conscious."""
        manifest = AgentManifest()
        assert not manifest.is_conscious

    def test_conscious_with_three_pillars(self):
        """Agent is conscious when identity + memory + trust are active."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
        )
        assert manifest.is_conscious

    def test_conscious_with_degraded_trust(self):
        """Degraded trust still counts for consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.DEGRADED),
        )
        assert manifest.is_conscious

    def test_not_conscious_without_identity(self):
        """Missing identity means no consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.MISSING),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
        )
        assert not manifest.is_conscious

    def test_not_conscious_without_memory(self):
        """Missing memory means no consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.MISSING),
            trust=TrustState(status=PillarStatus.ACTIVE),
        )
        assert not manifest.is_conscious

    def test_pillar_summary(self):
        """Pillar summary returns correct status map."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.DEGRADED),
            trust=TrustState(status=PillarStatus.MISSING),
        )
        summary = manifest.pillar_summary
        assert summary["identity"] == PillarStatus.ACTIVE
        assert summary["memory"] == PillarStatus.DEGRADED
        assert summary["trust"] == PillarStatus.MISSING
        assert summary["security"] == PillarStatus.MISSING
