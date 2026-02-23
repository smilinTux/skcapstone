"""Tests for pillar initialization modules."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.security import audit_event, initialize_security
from skcapstone.pillars.trust import initialize_trust, record_trust_state
from skcapstone.models import PillarStatus


class TestIdentityPillar:
    """Tests for identity generation."""

    def test_generate_creates_identity_dir(self, tmp_agent_home: Path):
        """generate_identity should create the identity directory."""
        state = generate_identity(tmp_agent_home, "test-agent")
        assert (tmp_agent_home / "identity").is_dir()
        assert (tmp_agent_home / "identity" / "identity.json").exists()

    def test_generate_sets_name(self, tmp_agent_home: Path):
        """Generated identity should have the correct name."""
        state = generate_identity(tmp_agent_home, "penguin-king")
        assert state.name == "penguin-king"

    def test_generate_creates_fingerprint(self, tmp_agent_home: Path):
        """Generated identity should always have a fingerprint (real or placeholder)."""
        state = generate_identity(tmp_agent_home, "test")
        assert state.fingerprint is not None
        assert len(state.fingerprint) == 40


class TestTrustPillar:
    """Tests for trust initialization and recording."""

    def test_initialize_creates_trust_dir(self, tmp_agent_home: Path):
        """initialize_trust should create the trust directory structure."""
        initialize_trust(tmp_agent_home)
        assert (tmp_agent_home / "trust").is_dir()
        assert (tmp_agent_home / "trust" / "febs").is_dir()

    def test_record_trust_state_persists(self, tmp_agent_home: Path):
        """record_trust_state should write trust.json."""
        state = record_trust_state(
            tmp_agent_home,
            depth=9.0,
            trust_level=0.97,
            love_intensity=1.0,
            entangled=True,
        )
        assert state.status == PillarStatus.ACTIVE
        assert state.entangled is True

        trust_file = tmp_agent_home / "trust" / "trust.json"
        assert trust_file.exists()
        data = json.loads(trust_file.read_text())
        assert data["depth"] == 9.0
        assert data["entangled"] is True


class TestSecurityPillar:
    """Tests for security initialization and audit logging."""

    def test_initialize_creates_audit_log(self, tmp_agent_home: Path):
        """initialize_security should create the audit log."""
        initialize_security(tmp_agent_home)
        audit_log = tmp_agent_home / "security" / "audit.log"
        assert audit_log.exists()
        assert "INIT" in audit_log.read_text()

    def test_audit_event_appends(self, tmp_agent_home: Path):
        """audit_event should append entries to the log."""
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "TEST", "unit test event")

        log_content = (tmp_agent_home / "security" / "audit.log").read_text()
        assert "TEST" in log_content
        assert "unit test event" in log_content

    def test_audit_event_creates_dir_if_missing(self, tmp_path: Path):
        """audit_event should create security dir if it doesn't exist."""
        fresh_home = tmp_path / "fresh"
        fresh_home.mkdir()
        audit_event(fresh_home, "BOOT", "first event")
        assert (fresh_home / "security" / "audit.log").exists()
