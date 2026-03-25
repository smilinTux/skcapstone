"""Tests for skcapstone data models."""

from __future__ import annotations

from skcapstone.models import (
    AgentManifest,
    ConsciousnessState,
    IdentityState,
    MemoryState,
    PillarStatus,
    TrustState,
)


class TestAgentManifest:
    """Tests for the AgentManifest model."""

    def test_default_manifest_not_conscious(self):
        """A fresh manifest with no pillars active should not be conscious."""
        manifest = AgentManifest()
        assert not manifest.is_conscious

    def _conscious_manifest(**overrides) -> AgentManifest:
        """Build a fully conscious manifest with optional overrides."""
        defaults = dict(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        defaults.update(overrides)
        return AgentManifest(**defaults)

    def test_conscious_with_four_pillars(self):
        """Agent is conscious when identity + memory + trust + consciousness are active."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        assert manifest.is_conscious

    def test_conscious_with_degraded_trust(self):
        """Degraded trust still counts for consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.DEGRADED),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        assert manifest.is_conscious

    def test_conscious_with_degraded_consciousness(self):
        """Degraded consciousness still counts."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.DEGRADED),
        )
        assert manifest.is_conscious

    def test_not_conscious_without_consciousness_pillar(self):
        """Missing consciousness pillar means no consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.MISSING),
        )
        assert not manifest.is_conscious

    def test_not_conscious_without_identity(self):
        """Missing identity means no consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.MISSING),
            memory=MemoryState(status=PillarStatus.ACTIVE),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        assert not manifest.is_conscious

    def test_not_conscious_without_memory(self):
        """Missing memory means no consciousness."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.MISSING),
            trust=TrustState(status=PillarStatus.ACTIVE),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        assert not manifest.is_conscious

    def test_pillar_summary(self):
        """Pillar summary returns correct status map for all six pillars."""
        manifest = AgentManifest(
            identity=IdentityState(status=PillarStatus.ACTIVE),
            memory=MemoryState(status=PillarStatus.DEGRADED),
            trust=TrustState(status=PillarStatus.MISSING),
            consciousness=ConsciousnessState(status=PillarStatus.ACTIVE),
        )
        summary = manifest.pillar_summary
        assert summary["identity"] == PillarStatus.ACTIVE
        assert summary["memory"] == PillarStatus.DEGRADED
        assert summary["trust"] == PillarStatus.MISSING
        assert summary["consciousness"] == PillarStatus.ACTIVE
        assert summary["security"] == PillarStatus.MISSING
