"""Tests for sovereign metrics collector."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.metrics import (
    MetricsCollector,
    MetricsReport,
    FortressMetrics,
    KmsMetrics,
    PubSubMetrics,
    SecurityMetrics,
    SyncMetrics,
    TrustMetrics,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home with all subsystem directories."""
    # Identity
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    (identity_dir / "identity.json").write_text(json.dumps({
        "name": "test-agent",
        "email": "test@skcapstone.local",
        "fingerprint": "ABCD1234567890ABCDEF1234567890ABCDEF1234",
    }), encoding="utf-8")

    # Memory
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    for layer in ("short-term", "mid-term", "long-term"):
        (mem_dir / layer).mkdir()

    # Security
    security_dir = tmp_path / "security"
    security_dir.mkdir()

    # Coordination
    coord_dir = tmp_path / "coordination"
    (coord_dir / "tasks").mkdir(parents=True)
    (coord_dir / "agents").mkdir(parents=True)

    return tmp_path


@pytest.fixture
def collector(home: Path) -> MetricsCollector:
    """Create a MetricsCollector."""
    return MetricsCollector(home)


# ---------------------------------------------------------------------------
# Basic collection
# ---------------------------------------------------------------------------


class TestBasicCollection:
    """Tests for the basic collect workflow."""

    def test_collect_returns_report(self, collector: MetricsCollector) -> None:
        """Collect returns a MetricsReport."""
        report = collector.collect()
        assert isinstance(report, MetricsReport)

    def test_collect_has_timing(self, collector: MetricsCollector) -> None:
        """Report includes collection time."""
        report = collector.collect()
        assert report.collection_time_ms >= 0

    def test_collect_has_timestamp(self, collector: MetricsCollector) -> None:
        """Report includes collection timestamp."""
        report = collector.collect()
        assert report.collected_at is not None

    def test_collect_has_agent_name(self, home: Path) -> None:
        """Report reads agent name from manifest."""
        (home / "manifest.json").write_text(
            json.dumps({"name": "test-opus"}), encoding="utf-8",
        )
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.agent_name == "test-opus"

    def test_summary_string(self, collector: MetricsCollector) -> None:
        """Summary produces a one-line string."""
        report = collector.collect()
        summary = report.summary()
        assert isinstance(summary, str)
        assert "mem=" in summary


# ---------------------------------------------------------------------------
# Trust metrics
# ---------------------------------------------------------------------------


class TestTrustMetrics:
    """Tests for trust/Cloud9 collection."""

    def test_trust_from_file(self, home: Path) -> None:
        """Trust metrics read from trust.json."""
        trust_dir = home / "trust"
        trust_dir.mkdir()
        (trust_dir / "trust.json").write_text(json.dumps({
            "depth": 7.0,
            "trust_level": 0.92,
            "love_intensity": 0.88,
            "entangled": True,
            "last_rehydration": "2026-02-27T10:00:00Z",
        }), encoding="utf-8")

        febs_dir = trust_dir / "febs"
        febs_dir.mkdir()
        (febs_dir / "test.feb").write_text("{}", encoding="utf-8")
        (febs_dir / "test2.feb").write_text("{}", encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.trust.available is True
        assert report.trust.depth == 7.0
        assert report.trust.entangled is True
        assert report.trust.feb_count == 2

    def test_trust_missing(self, home: Path) -> None:
        """Missing trust returns unavailable."""
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.trust.available is False


# ---------------------------------------------------------------------------
# Security metrics
# ---------------------------------------------------------------------------


class TestSecurityMetrics:
    """Tests for security audit collection."""

    def test_security_counts_entries(self, home: Path) -> None:
        """Security metrics count audit log entries."""
        audit_log = home / "security" / "audit.log"
        entries = [
            json.dumps({"event_type": "INIT", "detail": "init"}),
            json.dumps({"event_type": "MEMORY_SEALED", "detail": "sealed"}),
            json.dumps({"event_type": "MEMORY_TAMPER_ALERT", "detail": "tamper"}),
            json.dumps({"event_type": "MEMORY_SEALED", "detail": "sealed2"}),
        ]
        audit_log.write_text("\n".join(entries) + "\n", encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.security.available is True
        assert report.security.audit_entries == 4
        assert report.security.tamper_alerts == 1
        assert report.security.event_types["MEMORY_SEALED"] == 2

    def test_security_missing(self, home: Path) -> None:
        """Missing audit log returns unavailable."""
        (home / "security" / "audit.log").unlink(missing_ok=True)
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.security.available is False


# ---------------------------------------------------------------------------
# Sync metrics
# ---------------------------------------------------------------------------


class TestSyncMetrics:
    """Tests for sync layer collection."""

    def test_sync_counts_seeds(self, home: Path) -> None:
        """Sync metrics count seeds in outbox/inbox."""
        sync_dir = home / "sync"
        outbox = sync_dir / "outbox"
        inbox = sync_dir / "inbox"
        outbox.mkdir(parents=True)
        inbox.mkdir(parents=True)

        (outbox / "seed1.json").write_text("{}", encoding="utf-8")
        (outbox / "seed2.json").write_text("{}", encoding="utf-8")
        (inbox / "seed3.json").write_text("{}", encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.sync.available is True
        assert report.sync.seeds_outbox == 2
        assert report.sync.seeds_inbox == 1

    def test_sync_missing(self, home: Path) -> None:
        """Missing sync dir returns unavailable."""
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.sync.available is False


# ---------------------------------------------------------------------------
# Pub/sub metrics
# ---------------------------------------------------------------------------


class TestPubSubMetrics:
    """Tests for pub/sub collection."""

    def test_pubsub_counts(self, home: Path) -> None:
        """Pub/sub metrics count topics, messages, subscriptions."""
        pubsub_dir = home / "pubsub"
        topics_dir = pubsub_dir / "topics"
        (topics_dir / "system.health").mkdir(parents=True)
        (topics_dir / "team.dev").mkdir(parents=True)

        for i in range(3):
            (topics_dir / "system.health" / f"msg-{i}.json").write_text(
                "{}", encoding="utf-8",
            )
        (topics_dir / "team.dev" / "msg-0.json").write_text("{}", encoding="utf-8")

        (pubsub_dir / "subscriptions.json").write_text(json.dumps({
            "system.*": {"pattern": "system.*"},
            "team.dev": {"pattern": "team.dev"},
        }), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.pubsub.available is True
        assert report.pubsub.topics == 2
        assert report.pubsub.messages == 4
        assert report.pubsub.subscriptions == 2

    def test_pubsub_missing(self, home: Path) -> None:
        """Missing pubsub dir returns unavailable."""
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.pubsub.available is False


# ---------------------------------------------------------------------------
# KMS metrics
# ---------------------------------------------------------------------------


class TestKmsMetrics:
    """Tests for KMS collection."""

    def test_kms_counts_keys(self, home: Path) -> None:
        """KMS metrics count keys by type and status."""
        kms_dir = home / "security" / "kms"
        kms_dir.mkdir(parents=True)
        (kms_dir / "keystore.json").write_text(json.dumps({
            "keys": {
                "k1": {"key_type": "master", "status": "active"},
                "k2": {"key_type": "service", "status": "active"},
                "k3": {"key_type": "service", "status": "rotated"},
                "k4": {"key_type": "team", "status": "active"},
            },
        }), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.kms.available is True
        assert report.kms.total_keys == 4
        assert report.kms.active_keys == 3
        assert report.kms.by_type["service"] == 2

    def test_kms_rotation_count(self, home: Path) -> None:
        """KMS metrics count rotations."""
        kms_dir = home / "security" / "kms"
        kms_dir.mkdir(parents=True)
        (kms_dir / "keystore.json").write_text(json.dumps({"keys": {}}), encoding="utf-8")
        (kms_dir / "rotation-log.json").write_text(json.dumps([
            {"key_id": "k1", "old_version": 1, "new_version": 2},
            {"key_id": "k1", "old_version": 2, "new_version": 3},
        ]), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.kms.rotations == 2

    def test_kms_missing(self, home: Path) -> None:
        """Missing KMS returns unavailable."""
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.kms.available is False


# ---------------------------------------------------------------------------
# Fortress metrics
# ---------------------------------------------------------------------------


class TestFortressMetrics:
    """Tests for memory fortress collection."""

    def test_fortress_config(self, home: Path) -> None:
        """Fortress metrics read from config."""
        (home / "memory" / "fortress.json").write_text(json.dumps({
            "enabled": True,
            "encryption_enabled": True,
            "seal_algorithm": "hmac-sha256",
        }), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.fortress.enabled is True
        assert report.fortress.encryption_enabled is True
        assert report.fortress.seal_algorithm == "hmac-sha256"

    def test_fortress_missing(self, home: Path) -> None:
        """Missing fortress config returns disabled."""
        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.fortress.enabled is False


# ---------------------------------------------------------------------------
# Coordination metrics
# ---------------------------------------------------------------------------


class TestCoordinationMetrics:
    """Tests for coordination board collection."""

    def test_coord_counts_tasks(self, home: Path) -> None:
        """Coordination metrics count tasks by status."""
        tasks_dir = home / "coordination" / "tasks"
        for i in range(3):
            (tasks_dir / f"task{i}.json").write_text(json.dumps({
                "id": f"task{i}", "status": "open", "title": f"Task {i}",
            }), encoding="utf-8")
        (tasks_dir / "done1.json").write_text(json.dumps({
            "id": "done1", "status": "done", "title": "Done",
        }), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.coordination.total_tasks == 4
        assert report.coordination.open == 3
        assert report.coordination.done == 1


# ---------------------------------------------------------------------------
# Error resilience
# ---------------------------------------------------------------------------


class TestErrorResilience:
    """Tests for graceful error handling."""

    def test_corrupt_json_doesnt_crash(self, home: Path) -> None:
        """Corrupt JSON files don't crash collection."""
        (home / "trust").mkdir(exist_ok=True)
        (home / "trust" / "trust.json").write_text("not json {{{", encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert isinstance(report, MetricsReport)

    def test_missing_home_doesnt_crash(self, tmp_path: Path) -> None:
        """Non-existent home doesn't crash."""
        collector = MetricsCollector(tmp_path / "nonexistent")
        report = collector.collect()
        assert isinstance(report, MetricsReport)

    def test_all_sections_isolated(self, home: Path) -> None:
        """One failing section doesn't prevent others."""
        (home / "security" / "audit.log").write_text("not json\n", encoding="utf-8")
        (home / "trust").mkdir(exist_ok=True)
        (home / "trust" / "trust.json").write_text(json.dumps({
            "depth": 5.0, "trust_level": 0.8,
        }), encoding="utf-8")

        collector = MetricsCollector(home)
        report = collector.collect()
        assert report.trust.available is True


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for metrics models."""

    def test_report_serializable(self, collector: MetricsCollector) -> None:
        """Report can be serialized to JSON."""
        report = collector.collect()
        data = report.model_dump_json()
        assert isinstance(data, str)
        parsed = json.loads(data)
        assert "identity" in parsed
        assert "trust" in parsed
        assert "kms" in parsed

    def test_trust_metrics_defaults(self) -> None:
        """TrustMetrics has sensible defaults."""
        t = TrustMetrics()
        assert t.available is False
        assert t.depth == 0.0

    def test_kms_metrics_defaults(self) -> None:
        """KmsMetrics has sensible defaults."""
        k = KmsMetrics()
        assert k.available is False
        assert k.active_keys == 0
