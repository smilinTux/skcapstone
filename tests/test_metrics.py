"""Tests for sovereign metrics collector."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from skcapstone.metrics import (
    ConsciousnessMetrics,
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


# ---------------------------------------------------------------------------
# ConsciousnessMetrics
# ---------------------------------------------------------------------------


class TestConsciousnessMetrics:
    """Tests for the consciousness loop runtime metrics collector."""

    @pytest.fixture
    def cm(self, tmp_path: Path) -> ConsciousnessMetrics:
        """ConsciousnessMetrics with no background thread."""
        return ConsciousnessMetrics(home=tmp_path, persist_interval=0)

    # ------------------------------------------------------------------
    # Basic counters
    # ------------------------------------------------------------------

    def test_initial_counters_zero(self, cm: ConsciousnessMetrics) -> None:
        """All counters start at zero."""
        d = cm.to_dict()
        assert d["messages_processed"] == 0
        assert d["responses_sent"] == 0
        assert d["errors"] == 0

    def test_record_message_increments(self, cm: ConsciousnessMetrics) -> None:
        """record_message increments messages_processed and peer counter."""
        cm.record_message("alice")
        cm.record_message("alice")
        cm.record_message("bob")
        d = cm.to_dict()
        assert d["messages_processed"] == 3
        assert d["messages_per_peer"]["alice"] == 2
        assert d["messages_per_peer"]["bob"] == 1

    def test_record_response_increments(self, cm: ConsciousnessMetrics) -> None:
        """record_response increments responses_sent, backend, and tier."""
        cm.record_response(120.5, "ollama", "fast")
        cm.record_response(80.0, "anthropic", "standard")
        cm.record_response(95.0, "ollama", "fast")
        d = cm.to_dict()
        assert d["responses_sent"] == 3
        assert d["backend_usage"]["ollama"] == 2
        assert d["backend_usage"]["anthropic"] == 1
        assert d["tier_usage"]["fast"] == 2
        assert d["tier_usage"]["standard"] == 1

    def test_record_error_increments(self, cm: ConsciousnessMetrics) -> None:
        """record_error increments the errors counter."""
        cm.record_error()
        cm.record_error()
        assert cm.to_dict()["errors"] == 2

    # ------------------------------------------------------------------
    # Histogram
    # ------------------------------------------------------------------

    def test_histogram_stats_empty(self, cm: ConsciousnessMetrics) -> None:
        """Histogram returns zeros when no samples."""
        stats = cm.to_dict()["response_time_ms"]
        assert stats["count"] == 0
        assert stats["min"] == 0.0
        assert stats["avg"] == 0.0
        assert stats["p99"] == 0.0

    def test_histogram_min_max_avg(self, cm: ConsciousnessMetrics) -> None:
        """Histogram computes min/max/avg correctly."""
        for ms in [10.0, 20.0, 30.0, 40.0, 50.0]:
            cm.record_response(ms, "passthrough", "fast")
        stats = cm.to_dict()["response_time_ms"]
        assert stats["min"] == 10.0
        assert stats["max"] == 50.0
        assert stats["avg"] == 30.0
        assert stats["count"] == 5

    def test_histogram_p99_single(self, cm: ConsciousnessMetrics) -> None:
        """p99 of a single sample equals that sample."""
        cm.record_response(42.0, "passthrough", "fast")
        stats = cm.to_dict()["response_time_ms"]
        assert stats["p99"] == 42.0

    def test_histogram_p99_hundred_samples(self, cm: ConsciousnessMetrics) -> None:
        """p99 of 100 evenly-spaced samples is the 99th value."""
        for i in range(1, 101):
            cm.record_response(float(i), "passthrough", "fast")
        stats = cm.to_dict()["response_time_ms"]
        # p99_idx = min(99, int(100 * 0.99)) = 98 → sorted[98] = 99.0
        assert stats["p99"] == 99.0

    def test_histogram_capped_at_1000(self, cm: ConsciousnessMetrics) -> None:
        """Histogram caps sample list at 1 000 to bound memory."""
        for i in range(1200):
            cm.record_response(float(i), "passthrough", "fast")
        assert cm.to_dict()["response_time_ms"]["count"] == 1000

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def test_save_and_reload(self, tmp_path: Path) -> None:
        """save() writes JSON; a new instance with the same home loads it."""
        cm1 = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        cm1.record_message("peer-a")
        cm1.record_response(55.0, "ollama", "fast")
        cm1.record_error()
        cm1.save()

        cm2 = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        d = cm2.to_dict()
        assert d["messages_processed"] == 1
        assert d["responses_sent"] == 1
        assert d["errors"] == 1
        assert d["backend_usage"]["ollama"] == 1
        assert d["tier_usage"]["fast"] == 1
        assert d["messages_per_peer"]["peer-a"] == 1

    def test_save_creates_daily_file(self, tmp_path: Path) -> None:
        """save() creates the daily JSON file under metrics/daily/."""
        from datetime import datetime, timezone
        cm = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        cm.record_message("x")
        cm.save()

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = tmp_path / "metrics" / "daily" / f"{date_str}.json"
        assert daily.exists()
        data = json.loads(daily.read_text(encoding="utf-8"))
        assert data["messages_processed"] == 1

    def test_load_missing_file_doesnt_crash(self, tmp_path: Path) -> None:
        """Creating ConsciousnessMetrics when no file exists doesn't fail."""
        cm = ConsciousnessMetrics(home=tmp_path / "nonexistent", persist_interval=0)
        assert cm.to_dict()["messages_processed"] == 0

    def test_load_corrupt_file_doesnt_crash(self, tmp_path: Path) -> None:
        """Corrupt daily JSON is silently ignored."""
        from datetime import datetime, timezone
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = tmp_path / "metrics" / "daily" / f"{date_str}.json"
        daily.parent.mkdir(parents=True)
        daily.write_text("not json {{{", encoding="utf-8")
        cm = ConsciousnessMetrics(home=tmp_path, persist_interval=0)
        assert cm.to_dict()["messages_processed"] == 0

    # ------------------------------------------------------------------
    # Thread safety
    # ------------------------------------------------------------------

    def test_concurrent_record_message(self, cm: ConsciousnessMetrics) -> None:
        """Concurrent record_message calls produce correct total."""
        n = 200
        barrier = threading.Barrier(n)

        def _record():
            barrier.wait()
            cm.record_message("stress-peer")

        threads = [threading.Thread(target=_record) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        d = cm.to_dict()
        assert d["messages_processed"] == n
        assert d["messages_per_peer"]["stress-peer"] == n

    def test_concurrent_record_response(self, cm: ConsciousnessMetrics) -> None:
        """Concurrent record_response calls produce correct total."""
        n = 100
        barrier = threading.Barrier(n)

        def _record():
            barrier.wait()
            cm.record_response(10.0, "passthrough", "fast")

        threads = [threading.Thread(target=_record) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cm.to_dict()["responses_sent"] == n

    # ------------------------------------------------------------------
    # to_dict structure
    # ------------------------------------------------------------------

    def test_to_dict_keys(self, cm: ConsciousnessMetrics) -> None:
        """to_dict returns all required keys."""
        d = cm.to_dict()
        required = {
            "date", "session_start", "messages_processed", "responses_sent",
            "errors", "response_time_ms", "backend_usage", "tier_usage",
            "messages_per_peer",
        }
        assert required.issubset(d.keys())

    def test_to_dict_json_serializable(self, cm: ConsciousnessMetrics) -> None:
        """to_dict output can be serialized to JSON."""
        cm.record_message("peer")
        cm.record_response(50.0, "ollama", "local")
        data = json.dumps(cm.to_dict())
        assert isinstance(data, str)

    # ------------------------------------------------------------------
    # Peer name sanitization
    # ------------------------------------------------------------------

    def test_peer_name_truncated_to_64(self, cm: ConsciousnessMetrics) -> None:
        """Peer names longer than 64 chars are truncated in the counter key."""
        long_peer = "a" * 100
        cm.record_message(long_peer)
        d = cm.to_dict()
        assert "a" * 64 in d["messages_per_peer"]
        assert long_peer not in d["messages_per_peer"]
