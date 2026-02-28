"""Unit tests for the security pillar module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.models import PillarStatus
from skcapstone.pillars.security import (
    AUDIT_LOG_NAME,
    AuditEntry,
    audit_event,
    initialize_security,
    read_audit_log,
)


class TestInitializeSecurity:
    """Tests for initialize_security()."""

    def test_creates_security_directory(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        assert (tmp_agent_home / "security").is_dir()

    def test_creates_audit_log(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        assert (tmp_agent_home / "security" / AUDIT_LOG_NAME).exists()

    def test_audit_log_has_init_entry(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        log_path = tmp_agent_home / "security" / AUDIT_LOG_NAME
        first_line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
        entry = json.loads(first_line)
        assert entry["event_type"] == "INIT"

    def test_audit_log_init_entry_has_timestamp(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        log_path = tmp_agent_home / "security" / AUDIT_LOG_NAME
        entry = json.loads(log_path.read_text().strip().splitlines()[0])
        assert "timestamp" in entry and entry["timestamp"]

    def test_audit_log_init_entry_has_host(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        log_path = tmp_agent_home / "security" / AUDIT_LOG_NAME
        entry = json.loads(log_path.read_text().strip().splitlines()[0])
        assert "host" in entry and entry["host"]

    def test_returns_security_state(self, tmp_agent_home: Path):
        state = initialize_security(tmp_agent_home)
        assert state is not None

    def test_idempotent_does_not_duplicate_init_entry(self, tmp_agent_home: Path):
        """Calling initialize_security twice must not write a second INIT entry."""
        initialize_security(tmp_agent_home)
        initialize_security(tmp_agent_home)
        log_path = tmp_agent_home / "security" / AUDIT_LOG_NAME
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1


class TestAuditEvent:
    """Tests for audit_event()."""

    def test_returns_audit_entry(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entry = audit_event(tmp_agent_home, "TEST", "a test event")
        assert isinstance(entry, AuditEntry)

    def test_event_type_stored(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entry = audit_event(tmp_agent_home, "AUTH", "key verified")
        assert entry.event_type == "AUTH"

    def test_detail_stored(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entry = audit_event(tmp_agent_home, "AUTH", "key verified")
        assert entry.detail == "key verified"

    def test_entry_appended_to_log(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "SYNC_PUSH", "seed pushed")
        lines = (tmp_agent_home / "security" / AUDIT_LOG_NAME).read_text().splitlines()
        assert len(lines) == 2
        second = json.loads(lines[1])
        assert second["event_type"] == "SYNC_PUSH"

    def test_agent_field_stored(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entry = audit_event(tmp_agent_home, "BOOT", "agent started", agent="opus")
        assert entry.agent == "opus"
        lines = (tmp_agent_home / "security" / AUDIT_LOG_NAME).read_text().splitlines()
        assert json.loads(lines[-1])["agent"] == "opus"

    def test_metadata_field_stored(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        meta = {"token_id": "tok123", "caps": ["read"]}
        entry = audit_event(tmp_agent_home, "TOKEN_ISSUE", "token issued", metadata=meta)
        assert entry.metadata["token_id"] == "tok123"
        last = json.loads(
            (tmp_agent_home / "security" / AUDIT_LOG_NAME).read_text().splitlines()[-1]
        )
        assert last["metadata"]["token_id"] == "tok123"

    def test_creates_security_dir_if_missing(self, tmp_path: Path):
        fresh_home = tmp_path / "fresh"
        fresh_home.mkdir()
        audit_event(fresh_home, "BOOT", "first event")
        assert (fresh_home / "security" / AUDIT_LOG_NAME).exists()

    def test_multiple_events_accumulate(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        for i in range(3):
            audit_event(tmp_agent_home, "EVENT", f"entry {i}")
        lines = (tmp_agent_home / "security" / AUDIT_LOG_NAME).read_text().splitlines()
        assert len(lines) == 4  # 1 INIT + 3 events


class TestReadAuditLog:
    """Tests for read_audit_log()."""

    def test_empty_when_log_missing(self, tmp_path: Path):
        entries = read_audit_log(tmp_path / "no-home")
        assert entries == []

    def test_returns_list_of_audit_entries(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entries = read_audit_log(tmp_agent_home)
        assert isinstance(entries, list)
        assert all(isinstance(e, AuditEntry) for e in entries)

    def test_parses_init_entry(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        entries = read_audit_log(tmp_agent_home)
        assert len(entries) >= 1
        assert entries[0].event_type == "INIT"

    def test_reads_all_events_in_order(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "AUTH", "key check")
        audit_event(tmp_agent_home, "SYNC_PUSH", "seed sent")
        entries = read_audit_log(tmp_agent_home)
        assert len(entries) == 3
        assert entries[0].event_type == "INIT"
        assert entries[1].event_type == "AUTH"
        assert entries[2].event_type == "SYNC_PUSH"

    def test_limit_returns_newest_n(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        for i in range(5):
            audit_event(tmp_agent_home, "EVENT", f"entry {i}")
        entries = read_audit_log(tmp_agent_home, limit=2)
        assert len(entries) == 2
        assert "entry 3" in entries[0].detail
        assert "entry 4" in entries[1].detail

    def test_handles_legacy_plaintext_lines(self, tmp_agent_home: Path):
        security_dir = tmp_agent_home / "security"
        security_dir.mkdir(parents=True, exist_ok=True)
        (security_dir / AUDIT_LOG_NAME).write_text(
            "[2026-01-01T00:00:00] INIT — legacy format\n"
            "[2026-01-01T00:01:00] AUTH — old auth event\n",
            encoding="utf-8",
        )
        entries = read_audit_log(tmp_agent_home)
        assert len(entries) == 2
        assert all(e.event_type == "LEGACY" for e in entries)

    def test_mixed_jsonl_and_legacy(self, tmp_agent_home: Path):
        """Log may contain a mix of old plain-text and new JSONL entries."""
        initialize_security(tmp_agent_home)
        log_path = tmp_agent_home / "security" / AUDIT_LOG_NAME
        # Append a legacy line after the JSONL INIT entry
        with log_path.open("a") as f:
            f.write("[legacy] some old plain text event\n")
        entries = read_audit_log(tmp_agent_home)
        assert len(entries) == 2
        assert entries[0].event_type == "INIT"
        assert entries[1].event_type == "LEGACY"

    def test_limit_zero_returns_all(self, tmp_agent_home: Path):
        initialize_security(tmp_agent_home)
        audit_event(tmp_agent_home, "A", "a")
        audit_event(tmp_agent_home, "B", "b")
        entries = read_audit_log(tmp_agent_home, limit=0)
        assert len(entries) == 3


class TestAuditEntryModel:
    """Tests for the AuditEntry model."""

    def test_default_timestamp_is_set(self):
        entry = AuditEntry(event_type="TEST", detail="x")
        assert entry.timestamp

    def test_default_host_is_set(self):
        entry = AuditEntry(event_type="TEST", detail="x")
        assert entry.host

    def test_optional_agent_is_none_by_default(self):
        entry = AuditEntry(event_type="TEST", detail="x")
        assert entry.agent is None

    def test_optional_metadata_is_none_by_default(self):
        entry = AuditEntry(event_type="TEST", detail="x")
        assert entry.metadata is None

    def test_model_dump_json_round_trip(self):
        entry = AuditEntry(event_type="SYNC", detail="pushed seed", agent="opus")
        reloaded = AuditEntry.model_validate(json.loads(entry.model_dump_json()))
        assert reloaded.event_type == "SYNC"
        assert reloaded.agent == "opus"
