"""Tests for the daemon health loop and self-healing integration."""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.daemon import DaemonConfig, DaemonService, DaemonState


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon_home(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    (home / "config").mkdir()
    (home / "logs").mkdir()
    return home


SAMPLE_REPORT = {
    "timestamp": "2026-03-02T00:00:00+00:00",
    "checks_run": 5,
    "checks_passed": 4,
    "auto_fixed": 1,
    "still_broken": 0,
    "escalated": [],
    "details": [],
}

CRITICAL_REPORT = {
    "timestamp": "2026-03-02T01:00:00+00:00",
    "checks_run": 5,
    "checks_passed": 3,
    "auto_fixed": 0,
    "still_broken": 2,
    "escalated": ["memory_index", "sync_manifest"],
    "details": [],
}


def _run_healing_loop_once(svc: DaemonService) -> None:
    """Drive _healing_loop through exactly one work iteration by patching wait."""
    iterations = [0]

    def fast_wait(timeout=None):
        iterations[0] += 1
        if iterations[0] >= 2:
            svc._stop_event.set()

    with patch.object(svc._stop_event, "wait", side_effect=fast_wait):
        svc._healing_loop()


# ---------------------------------------------------------------------------
# DaemonState healing history
# ---------------------------------------------------------------------------

class TestDaemonStateHealingHistory:
    """Tests for healing_history tracking and record_healing_run()."""

    def test_healing_history_initially_empty(self):
        state = DaemonState()
        assert state.healing_history == []

    def test_record_healing_run_sets_self_healing_report(self):
        state = DaemonState()
        state.record_healing_run(SAMPLE_REPORT)
        assert state.self_healing_report is SAMPLE_REPORT

    def test_record_healing_run_appends_to_history(self):
        state = DaemonState()
        state.record_healing_run(SAMPLE_REPORT)
        state.record_healing_run(CRITICAL_REPORT)
        assert len(state.healing_history) == 2
        assert state.healing_history[0] is SAMPLE_REPORT
        assert state.healing_history[1] is CRITICAL_REPORT

    def test_healing_history_capped_at_20(self):
        state = DaemonState()
        for i in range(25):
            state.record_healing_run({**SAMPLE_REPORT, "checks_run": i})
        assert len(state.healing_history) == 20
        # Most recent should be last
        assert state.healing_history[-1]["checks_run"] == 24

    def test_snapshot_includes_self_healing_history(self):
        state = DaemonState()
        state.record_healing_run(SAMPLE_REPORT)
        snap = state.snapshot()
        assert "self_healing_history" in snap
        assert len(snap["self_healing_history"]) == 1

    def test_snapshot_history_capped_at_5(self):
        state = DaemonState()
        for i in range(10):
            state.record_healing_run({**SAMPLE_REPORT, "checks_run": i})
        snap = state.snapshot()
        assert len(snap["self_healing_history"]) == 5

    def test_record_healing_run_thread_safe(self):
        state = DaemonState()
        errors = []

        def worker():
            try:
                for _ in range(50):
                    state.record_healing_run(SAMPLE_REPORT)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(state.healing_history) <= 20


# ---------------------------------------------------------------------------
# _healing_loop behaviour
# ---------------------------------------------------------------------------

class TestHealingLoop:
    """Tests for DaemonService._healing_loop integration."""

    def test_healing_loop_calls_diagnose_and_heal(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.return_value = SAMPLE_REPORT
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        _run_healing_loop_once(svc)

        mock_healer.diagnose_and_heal.assert_called_once()

    def test_healing_loop_stores_report_in_state(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.return_value = SAMPLE_REPORT
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        _run_healing_loop_once(svc)

        assert svc.state.self_healing_report["checks_run"] == SAMPLE_REPORT["checks_run"]

    def test_healing_loop_appends_to_history(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.return_value = SAMPLE_REPORT
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        _run_healing_loop_once(svc)

        assert len(svc.state.healing_history) == 1

    def test_healing_loop_handles_exception_gracefully(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.side_effect = RuntimeError("kaboom")
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        # Must not propagate
        _run_healing_loop_once(svc)

        assert any("Self-healing" in e for e in svc.state.errors)

    def test_healing_loop_logs_warning_for_critical_issues(self, daemon_home, caplog):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.return_value = CRITICAL_REPORT
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        with caplog.at_level(logging.WARNING, logger="skcapstone.daemon"):
            _run_healing_loop_once(svc)

        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("critical" in m.lower() for m in warning_msgs)

    def test_healing_loop_no_warning_when_all_ok(self, daemon_home, caplog):
        ok_report = {**SAMPLE_REPORT, "still_broken": 0, "auto_fixed": 0}
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        mock_healer = MagicMock()
        mock_healer.diagnose_and_heal.return_value = ok_report
        svc._healer = mock_healer
        svc._stop_event = threading.Event()

        with caplog.at_level(logging.WARNING, logger="skcapstone.daemon"):
            _run_healing_loop_once(svc)

        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_msgs == []

    def test_healing_loop_skips_cleanly_when_no_healer(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        svc._healer = None
        svc._stop_event = threading.Event()

        _run_healing_loop_once(svc)  # must not raise

        assert svc.state.self_healing_report == {}

    def test_healing_loop_updates_consciousness_stats(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        svc._healer = None
        mock_consciousness = MagicMock()
        mock_consciousness.stats = {"enabled": True, "messages_processed": 7}
        svc._consciousness = mock_consciousness
        svc._stop_event = threading.Event()

        _run_healing_loop_once(svc)

        assert svc.state.consciousness_stats["messages_processed"] == 7


# ---------------------------------------------------------------------------
# GET /api/v1/health endpoint
# ---------------------------------------------------------------------------

class TestHealthAPIEndpoint:
    """Tests for the comprehensive /api/v1/health HTTP endpoint."""

    def _start_server(self, daemon_home, healing_report=None):
        config = DaemonConfig(
            home=daemon_home, shared_root=daemon_home, port=0, poll_interval=60
        )
        svc = DaemonService(config)
        svc.state.running = True
        svc.state.started_at = datetime.now(timezone.utc)
        if healing_report:
            svc.state.record_healing_run(healing_report)
        with patch.object(svc, "_load_components"):
            svc.config.port = _find_free_port()
            svc._write_pid()
            svc._start_api_server()
        time.sleep(0.3)
        return svc

    def _get(self, port, path):
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status, json.loads(resp.read())

    def test_health_endpoint_returns_200(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, _ = self._get(svc.config.port, "/api/v1/health")
            assert status == 200
        finally:
            svc.stop()

    def test_health_endpoint_required_fields(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert "uptime_seconds" in data
            assert "daemon_pid" in data
            assert "consciousness_enabled" in data
            assert "self_healing_last_run" in data
            assert "self_healing_issues_found" in data
            assert "self_healing_auto_fixed" in data
            assert "backend_health" in data
            assert "disk_free_gb" in data
            assert "memory_usage_mb" in data
        finally:
            svc.stop()

    def test_health_endpoint_status_ok_when_running(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["status"] == "ok"
        finally:
            svc.stop()

    def test_health_endpoint_self_healing_data_from_last_run(self, daemon_home):
        svc = self._start_server(daemon_home, healing_report=SAMPLE_REPORT)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["self_healing_last_run"] == SAMPLE_REPORT["timestamp"]
            assert data["self_healing_issues_found"] == 0
            assert data["self_healing_auto_fixed"] == 1
        finally:
            svc.stop()

    def test_health_endpoint_critical_issues_reflected(self, daemon_home):
        svc = self._start_server(daemon_home, healing_report=CRITICAL_REPORT)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["self_healing_issues_found"] == 2
            assert data["self_healing_auto_fixed"] == 0
        finally:
            svc.stop()

    def test_health_endpoint_no_healing_run_yet(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["self_healing_last_run"] is None
            assert data["self_healing_issues_found"] == 0
        finally:
            svc.stop()

    def test_health_endpoint_disk_free_non_negative(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["disk_free_gb"] >= 0
        finally:
            svc.stop()

    def test_health_endpoint_memory_usage_non_negative(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["memory_usage_mb"] >= 0
        finally:
            svc.stop()

    def test_health_endpoint_consciousness_false_when_not_loaded(self, daemon_home):
        svc = self._start_server(daemon_home)
        # _consciousness is None (not loaded)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["consciousness_enabled"] is False
        finally:
            svc.stop()

    def test_health_endpoint_daemon_pid_matches(self, daemon_home):
        import os
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/health")
            assert data["daemon_pid"] == os.getpid()
        finally:
            svc.stop()


# ---------------------------------------------------------------------------
# Profile freshness warning
# ---------------------------------------------------------------------------

class TestProfileFreshnessWarning:
    """Tests that stale model profiles emit a WARNING log."""

    def test_stale_profile_triggers_warning(self, tmp_path, caplog):
        from skcapstone.self_healing import SelfHealingDoctor

        home = tmp_path / ".skcapstone"
        home.mkdir()
        doctor = SelfHealingDoctor(home)

        stale_profile = MagicMock()
        stale_profile.family = "gpt4-legacy"
        stale_profile.last_updated = "2020-01-01T00:00:00+00:00"

        mock_adapter = MagicMock()
        mock_adapter.profiles = [stale_profile]

        with patch("skcapstone.prompt_adapter.PromptAdapter", return_value=mock_adapter):
            with caplog.at_level(logging.WARNING, logger="skcapstone.self_healing"):
                result = doctor._check_profile_freshness()

        assert result["status"] == "ok"
        assert "stale_profiles" in result
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("90" in m or "stale" in m.lower() or "gpt4-legacy" in m for m in warning_msgs)

    def test_fresh_profile_no_warning(self, tmp_path, caplog):
        from skcapstone.self_healing import SelfHealingDoctor

        home = tmp_path / ".skcapstone"
        home.mkdir()
        doctor = SelfHealingDoctor(home)

        fresh_profile = MagicMock()
        fresh_profile.family = "llama3.2"
        fresh_profile.last_updated = datetime.now(timezone.utc).isoformat()

        mock_adapter = MagicMock()
        mock_adapter.profiles = [fresh_profile]

        with patch("skcapstone.prompt_adapter.PromptAdapter", return_value=mock_adapter):
            with caplog.at_level(logging.WARNING, logger="skcapstone.self_healing"):
                result = doctor._check_profile_freshness()

        assert result["status"] == "ok"
        warning_msgs = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning_msgs == []
