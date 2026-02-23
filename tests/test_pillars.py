"""Tests for pillar initialization modules."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.pillars.identity import generate_identity
from skcapstone.pillars.security import (
    AuditEntry,
    audit_event,
    initialize_security,
    read_audit_log,
)
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
        """initialize_security should create a structured JSONL audit log."""
        initialize_security(tmp_agent_home)
        audit_log = tmp_agent_home / "security" / "audit.log"
        assert audit_log.exists()

        line = audit_log.read_text().strip()
        data = json.loads(line)
        assert data["event_type"] == "INIT"
        assert "timestamp" in data
        assert "host" in data

    def test_audit_event_appends_structured(self, tmp_agent_home: Path):
        """audit_event should append structured JSON entries."""
        initialize_security(tmp_agent_home)
        entry = audit_event(tmp_agent_home, "TEST", "unit test event")

        assert isinstance(entry, AuditEntry)
        assert entry.event_type == "TEST"
        assert entry.detail == "unit test event"

        lines = (tmp_agent_home / "security" / "audit.log").read_text().splitlines()
        assert len(lines) == 2
        parsed = json.loads(lines[1])
        assert parsed["event_type"] == "TEST"
        assert parsed["detail"] == "unit test event"

    def test_audit_event_with_metadata(self, tmp_agent_home: Path):
        """audit_event should store optional agent and metadata fields."""
        initialize_security(tmp_agent_home)
        entry = audit_event(
            tmp_agent_home,
            "TOKEN_ISSUE",
            "Issued token abc123",
            agent="opus",
            metadata={"token_id": "abc123", "capabilities": ["read"]},
        )

        assert entry.agent == "opus"
        assert entry.metadata["token_id"] == "abc123"

        lines = (tmp_agent_home / "security" / "audit.log").read_text().splitlines()
        parsed = json.loads(lines[-1])
        assert parsed["agent"] == "opus"
        assert parsed["metadata"]["token_id"] == "abc123"

    def test_audit_event_creates_dir_if_missing(self, tmp_path: Path):
        """audit_event should create security dir if it doesn't exist."""
        fresh_home = tmp_path / "fresh"
        fresh_home.mkdir()
        audit_event(fresh_home, "BOOT", "first event")
        assert (fresh_home / "security" / "audit.log").exists()

    def test_read_audit_log_parses_entries(self, tmp_agent_home: Path):
        """read_audit_log should return structured AuditEntry objects."""
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "AUTH", "key verified")
        audit_event(tmp_agent_home, "SYNC_PUSH", "seed pushed")

        entries = read_audit_log(tmp_agent_home)
        assert len(entries) == 3
        assert entries[0].event_type == "INIT"
        assert entries[1].event_type == "AUTH"
        assert entries[2].event_type == "SYNC_PUSH"

    def test_read_audit_log_handles_legacy(self, tmp_agent_home: Path):
        """read_audit_log should gracefully handle old plain-text entries."""
        security_dir = tmp_agent_home / "security"
        security_dir.mkdir(parents=True, exist_ok=True)
        log = security_dir / "audit.log"
        log.write_text(
            "[2026-02-22T12:00:00+00:00] INIT — old format\n"
            "[2026-02-22T12:01:00+00:00] AUTH — legacy auth\n"
        )

        entries = read_audit_log(tmp_agent_home)
        assert len(entries) == 2
        assert all(e.event_type == "LEGACY" for e in entries)
        assert "old format" in entries[0].detail

    def test_read_audit_log_with_limit(self, tmp_agent_home: Path):
        """read_audit_log with limit returns only the newest N entries."""
        initialize_security(tmp_agent_home)
        for i in range(5):
            audit_event(tmp_agent_home, "EVENT", f"entry {i}")

        entries = read_audit_log(tmp_agent_home, limit=2)
        assert len(entries) == 2
        assert "entry 3" in entries[0].detail
        assert "entry 4" in entries[1].detail

    def test_read_audit_log_empty(self, tmp_path: Path):
        """read_audit_log returns empty list when no log exists."""
        entries = read_audit_log(tmp_path / "nonexistent")
        assert entries == []
