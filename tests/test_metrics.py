"""Tests for the sovereign metrics collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.metrics import (
    BackupMetrics,
    ChatMetrics,
    CoordinationMetrics,
    IdentityMetrics,
    MemoryMetrics,
    MetricsCollector,
    MetricsReport,
    TransportMetrics,
)


def _setup_agent_home(tmp_path: Path) -> Path:
    """Create a minimal agent home for testing."""
    home = tmp_path / ".skcapstone"

    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text('{"agent_name": "TestAgent"}')
    (home / "manifest.json").write_text('{"name": "TestAgent"}')

    tasks_dir = home / "coordination" / "tasks"
    tasks_dir.mkdir(parents=True)
    for i, status in enumerate(["done", "done", "done", "open", "in_progress"]):
        (tasks_dir / f"task-{i}.json").write_text(json.dumps({"status": status}))

    backups = home / "backups"
    backups.mkdir()
    (backups / "backup-20260224-120000-000000.tar.gz").write_bytes(b"x" * 1024)

    return home


class TestMetricsCollector:
    """Tests for the collector."""

    def test_collect_basic(self, tmp_path: Path) -> None:
        """Happy path: collect produces a report."""
        home = _setup_agent_home(tmp_path)
        collector = MetricsCollector(home=home)
        report = collector.collect()

        assert report.agent_name == "TestAgent"
        assert report.version == "0.1.0"
        assert report.collection_time_ms > 0

    def test_collect_coordination(self, tmp_path: Path) -> None:
        """Coordination metrics count tasks by status."""
        home = _setup_agent_home(tmp_path)
        collector = MetricsCollector(home=home)
        report = collector.collect()

        assert report.coordination.total_tasks == 5
        assert report.coordination.done == 3
        assert report.coordination.open == 1
        assert report.coordination.in_progress == 1

    def test_collect_backup(self, tmp_path: Path) -> None:
        """Backup metrics find the latest backup."""
        home = _setup_agent_home(tmp_path)
        collector = MetricsCollector(home=home)
        report = collector.collect()

        assert report.backup.backup_count == 1
        assert "backup-" in report.backup.latest_backup
        assert report.backup.latest_size_bytes == 1024

    def test_collect_empty_home(self, tmp_path: Path) -> None:
        """Collector handles non-existent home gracefully (no crash)."""
        collector = MetricsCollector(home=tmp_path / "nonexistent")
        report = collector.collect()

        assert report.agent_name == "unknown"
        assert len(report.errors) == 0
        assert report.collection_time_ms > 0

    def test_collect_no_backups(self, tmp_path: Path) -> None:
        """No backup directory produces zero counts."""
        home = tmp_path / ".skcapstone"
        home.mkdir(parents=True)
        collector = MetricsCollector(home=home)
        report = collector.collect()
        assert report.backup.backup_count == 0

    def test_uptime_tracked(self, tmp_path: Path) -> None:
        """Uptime is positive after collection."""
        collector = MetricsCollector(home=tmp_path)
        report = collector.collect()
        assert report.uptime_seconds >= 0


class TestMetricsReport:
    """Tests for the report model."""

    def test_summary_format(self) -> None:
        """Summary produces a readable one-liner."""
        report = MetricsReport(
            agent_name="Jarvis",
            identity=IdentityMetrics(available=True),
            memory=MemoryMetrics(total_memories=42),
            chat=ChatMetrics(total_messages=100),
            coordination=CoordinationMetrics(total_tasks=50, done=45),
        )
        summary = report.summary()
        assert "Jarvis" in summary
        assert "mem=42" in summary
        assert "chat=100" in summary
        assert "45/50" in summary

    def test_json_serialization(self) -> None:
        """Report serializes to JSON."""
        report = MetricsReport(agent_name="Test")
        data = json.loads(report.model_dump_json())
        assert data["agent_name"] == "Test"
        assert "collected_at" in data

    def test_errors_tracked(self) -> None:
        """Errors list captures collection failures."""
        report = MetricsReport(errors=["identity: file not found", "memory: timeout"])
        assert len(report.errors) == 2


class TestSubMetrics:
    """Tests for individual metric models."""

    def test_identity_defaults(self) -> None:
        """Identity defaults to unavailable."""
        m = IdentityMetrics()
        assert m.available is False
        assert m.fingerprint == ""

    def test_memory_defaults(self) -> None:
        """Memory defaults to zero counts."""
        m = MemoryMetrics()
        assert m.total_memories == 0
        assert m.store_size_bytes == 0

    def test_chat_defaults(self) -> None:
        """Chat defaults to zero."""
        m = ChatMetrics()
        assert m.total_messages == 0

    def test_transport_defaults(self) -> None:
        """Transport defaults to zero."""
        m = TransportMetrics()
        assert m.outbox_pending == 0

    def test_coordination_defaults(self) -> None:
        """Coordination defaults to zero."""
        m = CoordinationMetrics()
        assert m.total_tasks == 0

    def test_backup_defaults(self) -> None:
        """Backup defaults to empty."""
        m = BackupMetrics()
        assert m.backup_count == 0
        assert m.latest_backup == ""
