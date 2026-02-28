"""Unit tests for the identity pillar module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.models import PillarStatus
from skcapstone.pillars.identity import (
    _generate_placeholder_fingerprint,
    generate_identity,
)


class TestGenerateIdentity:
    """Tests for generate_identity()."""

    def test_creates_identity_directory(self, tmp_agent_home: Path):
        generate_identity(tmp_agent_home, "test-agent")
        assert (tmp_agent_home / "identity").is_dir()

    def test_writes_identity_json(self, tmp_agent_home: Path):
        generate_identity(tmp_agent_home, "test-agent")
        assert (tmp_agent_home / "identity" / "identity.json").exists()

    def test_state_name_matches(self, tmp_agent_home: Path):
        state = generate_identity(tmp_agent_home, "my-agent")
        assert state.name == "my-agent"

    def test_explicit_email_used(self, tmp_agent_home: Path):
        state = generate_identity(tmp_agent_home, "opus", email="opus@skworld.io")
        assert state.email == "opus@skworld.io"

    def test_email_auto_generated_from_name(self, tmp_agent_home: Path):
        state = generate_identity(tmp_agent_home, "My Agent")
        assert state.email == "my-agent@skcapstone.local"

    def test_fingerprint_always_set(self, tmp_agent_home: Path):
        state = generate_identity(tmp_agent_home, "test")
        assert state.fingerprint is not None
        assert len(state.fingerprint) == 40

    def test_identity_json_has_required_fields(self, tmp_agent_home: Path):
        generate_identity(tmp_agent_home, "agent-x")
        data = json.loads(
            (tmp_agent_home / "identity" / "identity.json").read_text()
        )
        for field in ("name", "email", "fingerprint", "created_at"):
            assert field in data, f"missing field: {field}"

    def test_identity_json_name_matches(self, tmp_agent_home: Path):
        generate_identity(tmp_agent_home, "lumina")
        data = json.loads(
            (tmp_agent_home / "identity" / "identity.json").read_text()
        )
        assert data["name"] == "lumina"

    def test_idempotent_second_call_succeeds(self, tmp_agent_home: Path):
        generate_identity(tmp_agent_home, "agent-a")
        state2 = generate_identity(tmp_agent_home, "agent-a")
        assert state2.name == "agent-a"

    def test_degraded_status_without_capauth(self, tmp_agent_home: Path):
        """Without capauth installed the status should be DEGRADED."""
        with patch.dict("sys.modules", {"capauth": None, "capauth.profile": None, "capauth.keys": None}):
            state = generate_identity(tmp_agent_home, "test-agent")
        assert state.status == PillarStatus.DEGRADED

    def test_capauth_managed_false_without_capauth(self, tmp_agent_home: Path):
        with patch.dict("sys.modules", {"capauth": None, "capauth.profile": None, "capauth.keys": None}):
            generate_identity(tmp_agent_home, "no-capauth")
        data = json.loads(
            (tmp_agent_home / "identity" / "identity.json").read_text()
        )
        assert data["capauth_managed"] is False

    def test_active_status_with_capauth_profile(self, tmp_agent_home: Path):
        """When capauth.profile.load_profile succeeds, status is ACTIVE."""
        mock_profile = MagicMock()
        mock_profile.key_info.fingerprint = "A" * 40
        mock_profile.key_info.public_key_path = str(tmp_agent_home / "agent.pub")
        mock_profile.entity.name = "opus"
        mock_profile.entity.email = "opus@test"

        mock_capauth_profile = MagicMock()
        mock_capauth_profile.load_profile.return_value = mock_profile

        with patch.dict("sys.modules", {"capauth": MagicMock(), "capauth.profile": mock_capauth_profile}):
            state = generate_identity(tmp_agent_home, "opus")

        assert state.status == PillarStatus.ACTIVE
        assert state.fingerprint == "A" * 40


class TestPlaceholderFingerprint:
    """Tests for _generate_placeholder_fingerprint()."""

    def test_length_is_40(self):
        fp = _generate_placeholder_fingerprint("test-agent")
        assert len(fp) == 40

    def test_is_uppercase_hex(self):
        fp = _generate_placeholder_fingerprint("agent")
        assert all(c in "0123456789ABCDEF" for c in fp)

    def test_deterministic(self):
        fp1 = _generate_placeholder_fingerprint("opus")
        fp2 = _generate_placeholder_fingerprint("opus")
        assert fp1 == fp2

    def test_different_names_different_fingerprints(self):
        fp1 = _generate_placeholder_fingerprint("opus")
        fp2 = _generate_placeholder_fingerprint("lumina")
        assert fp1 != fp2
