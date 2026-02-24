"""Tests for the skcapstone daemon."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.daemon import (
    DaemonConfig,
    DaemonService,
    DaemonState,
    is_running,
    read_pid,
)


@pytest.fixture
def daemon_home(tmp_path):
    """Create a minimal agent home for daemon tests."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    (home / "config").mkdir()
    (home / "logs").mkdir()
    return home


@pytest.fixture
def daemon_config(daemon_home):
    return DaemonConfig(
        home=daemon_home,
        poll_interval=1,
        sync_interval=60,
        health_interval=5,
        port=0,
    )


class TestDaemonState:
    """Tests for thread-safe DaemonState."""

    def test_initial_state(self):
        state = DaemonState()
        assert state.running is False
        assert state.messages_received == 0
        assert state.syncs_completed == 0

    def test_snapshot(self):
        state = DaemonState()
        snap = state.snapshot()
        assert snap["running"] is False
        assert snap["messages_received"] == 0
        assert "pid" in snap

    def test_record_poll(self):
        state = DaemonState()
        state.record_poll(3)
        assert state.messages_received == 3
        assert state.last_poll is not None

    def test_record_poll_accumulates(self):
        state = DaemonState()
        state.record_poll(2)
        state.record_poll(5)
        assert state.messages_received == 7

    def test_record_sync(self):
        state = DaemonState()
        state.record_sync()
        assert state.syncs_completed == 1
        assert state.last_sync is not None

    def test_record_health(self):
        state = DaemonState()
        state.record_health({"syncthing": {"status": "available"}})
        assert "syncthing" in state.health_reports

    def test_record_error(self):
        state = DaemonState()
        state.record_error("test error")
        assert len(state.errors) == 1

    def test_error_limit(self):
        state = DaemonState()
        for i in range(60):
            state.record_error(f"error-{i}")
        assert len(state.errors) == 50

    def test_thread_safety(self):
        state = DaemonState()
        errors = []

        def worker(n):
            try:
                for _ in range(100):
                    state.record_poll(1)
                    state.record_error(f"from-{n}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert state.messages_received == 400


class TestDaemonConfig:
    """Tests for DaemonConfig."""

    def test_defaults(self, daemon_home):
        config = DaemonConfig(home=daemon_home)
        assert config.poll_interval == 10
        assert config.sync_interval == 300
        assert config.health_interval == 60
        assert config.port == 7777

    def test_custom(self, daemon_home):
        config = DaemonConfig(home=daemon_home, poll_interval=5, port=9999)
        assert config.poll_interval == 5
        assert config.port == 9999

    def test_creates_log_dir(self, daemon_home):
        config = DaemonConfig(home=daemon_home)
        assert config.log_file.parent.exists()


class TestPidManagement:
    """Tests for PID file read/write."""

    def test_no_pid_file(self, daemon_home):
        assert read_pid(daemon_home) is None

    def test_is_running_false(self, daemon_home):
        assert is_running(daemon_home) is False

    def test_stale_pid_cleaned(self, daemon_home):
        pid_path = daemon_home / "daemon.pid"
        pid_path.write_text("999999999")
        assert read_pid(daemon_home) is None
        assert not pid_path.exists()


class TestDaemonService:
    """Tests for DaemonService lifecycle."""

    def test_creates_and_stops(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0, poll_interval=60)
        svc = DaemonService(config)

        # Patch _load_components to avoid importing skcomm/runtime
        with patch.object(svc, "_load_components"):
            with patch.object(svc, "_start_api_server"):
                svc.start()
                assert svc.state.running is True
                assert (daemon_home / "daemon.pid").exists()

                svc.stop()
                assert svc.state.running is False
                assert not (daemon_home / "daemon.pid").exists()

    def test_poll_loop_with_mock_skcomm(self, daemon_home):
        config = DaemonConfig(home=daemon_home, poll_interval=1, port=0)
        svc = DaemonService(config)

        mock_comm = MagicMock()
        mock_comm.receive.return_value = []
        svc._skcomm = mock_comm

        svc._stop_event = threading.Event()
        t = threading.Thread(target=svc._poll_loop, daemon=True)
        t.start()

        time.sleep(1.5)
        svc._stop_event.set()
        t.join(timeout=3)

        assert mock_comm.receive.called
        assert svc.state.last_poll is not None


class TestDaemonAPI:
    """Tests for the HTTP API server."""

    def test_api_ping(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0, poll_interval=60)
        svc = DaemonService(config)
        svc.state.running = True

        with patch.object(svc, "_load_components"):
            svc.config.port = _find_free_port()
            svc._write_pid()
            svc._start_api_server()

            time.sleep(0.5)

            try:
                url = f"http://127.0.0.1:{svc.config.port}/ping"
                with urllib.request.urlopen(url, timeout=2) as resp:
                    data = json.loads(resp.read())
                    assert data["pong"] is True
            finally:
                svc.stop()

    def test_api_status(self, daemon_home):
        config = DaemonConfig(home=daemon_home, port=0, poll_interval=60)
        svc = DaemonService(config)
        svc.state.running = True

        with patch.object(svc, "_load_components"):
            svc.config.port = _find_free_port()
            svc._write_pid()
            svc._start_api_server()

            time.sleep(0.5)

            try:
                url = f"http://127.0.0.1:{svc.config.port}/status"
                with urllib.request.urlopen(url, timeout=2) as resp:
                    data = json.loads(resp.read())
                    assert data["running"] is True
                    assert "pid" in data
            finally:
                svc.stop()


def _find_free_port() -> int:
    """Find an available port for testing."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
