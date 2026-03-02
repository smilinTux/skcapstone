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


class TestHeartbeatBeaconWiring:
    """Tests that HeartbeatBeacon is wired into the daemon health loop."""

    def test_beacon_defaults_to_none(self, daemon_home):
        """_beacon is None before _load_components runs."""
        config = DaemonConfig(home=daemon_home, port=0)
        svc = DaemonService(config)
        assert svc._beacon is None

    def test_health_loop_pulses_beacon_consciousness_active(self, daemon_home):
        """_health_loop calls beacon.pulse(consciousness_active=True) when consciousness is set."""
        config = DaemonConfig(home=daemon_home, port=0, health_interval=60)
        svc = DaemonService(config)

        mock_beacon = MagicMock()
        svc._beacon = mock_beacon
        svc._consciousness = MagicMock()  # truthy → consciousness_active=True

        svc._stop_event = threading.Event()
        t = threading.Thread(target=svc._health_loop, daemon=True)
        t.start()
        time.sleep(0.2)
        svc._stop_event.set()
        t.join(timeout=2)

        mock_beacon.pulse.assert_called_once()
        _, kwargs = mock_beacon.pulse.call_args
        assert kwargs["consciousness_active"] is True

    def test_health_loop_pulses_beacon_consciousness_inactive(self, daemon_home):
        """_health_loop calls beacon.pulse(consciousness_active=False) when consciousness is None."""
        config = DaemonConfig(home=daemon_home, port=0, health_interval=60)
        svc = DaemonService(config)

        mock_beacon = MagicMock()
        svc._beacon = mock_beacon
        svc._consciousness = None  # falsy → consciousness_active=False

        svc._stop_event = threading.Event()
        t = threading.Thread(target=svc._health_loop, daemon=True)
        t.start()
        time.sleep(0.2)
        svc._stop_event.set()
        t.join(timeout=2)

        mock_beacon.pulse.assert_called_once()
        _, kwargs = mock_beacon.pulse.call_args
        assert kwargs["consciousness_active"] is False

    def test_health_loop_skips_pulse_when_no_beacon(self, daemon_home):
        """_health_loop does not crash when _beacon is None."""
        config = DaemonConfig(home=daemon_home, port=0, health_interval=60)
        svc = DaemonService(config)
        svc._beacon = None

        svc._stop_event = threading.Event()
        t = threading.Thread(target=svc._health_loop, daemon=True)
        t.start()
        time.sleep(0.2)
        svc._stop_event.set()
        t.join(timeout=2)
        # No exception → test passes

    def test_load_components_initializes_beacon(self, daemon_home):
        """_load_components sets _beacon using sys.modules patching."""
        import sys

        config = DaemonConfig(home=daemon_home, port=0, consciousness_enabled=False)
        svc = DaemonService(config)

        mock_runtime = MagicMock()
        mock_runtime.manifest.name = "test-agent"
        mock_runtime.is_initialized = True

        mock_runtime_mod = MagicMock()
        mock_runtime_mod.get_runtime.return_value = mock_runtime

        mock_beacon_instance = MagicMock()
        mock_heartbeat_mod = MagicMock()
        mock_heartbeat_mod.HeartbeatBeacon.return_value = mock_beacon_instance

        patched = {
            "skcomm": MagicMock(),
            "skcomm.core": MagicMock(),
            "skcapstone.runtime": mock_runtime_mod,
            "skcapstone.heartbeat": mock_heartbeat_mod,
            "skcapstone.consciousness_config": MagicMock(),
            "skcapstone.consciousness_loop": MagicMock(),
            "skcapstone.self_healing": MagicMock(),
        }
        with patch.dict(sys.modules, patched):
            svc._load_components()

        assert svc._beacon is mock_beacon_instance
        mock_heartbeat_mod.HeartbeatBeacon.assert_called_once_with(
            config.home, "test-agent"
        )


class TestHouseholdAPI:
    """Tests for the household and conversation HTTP endpoints."""

    def _start_server(self, daemon_home):
        """Start the API server on a free port and return the service."""
        config = DaemonConfig(home=daemon_home, shared_root=daemon_home, port=0, poll_interval=60)
        svc = DaemonService(config)
        svc.state.running = True
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

    def _get_404(self, port, path):
        import urllib.error
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    # ── /api/v1/household/agents ─────────────────────────────────────────

    def test_household_agents_empty(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, data = self._get(svc.config.port, "/api/v1/household/agents")
            assert status == 200
            assert data["agents"] == []
        finally:
            svc.stop()

    def test_household_agents_with_identity(self, daemon_home):
        agent_dir = daemon_home / "agents" / "testbot"
        (agent_dir / "identity").mkdir(parents=True)
        (agent_dir / "identity" / "identity.json").write_text(
            json.dumps({"name": "Testbot", "fingerprint": "ABC123"})
        )
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/household/agents")
            assert len(data["agents"]) == 1
            agent = data["agents"][0]
            assert agent["name"] == "testbot"
            assert agent["identity"]["fingerprint"] == "ABC123"
            assert agent["status"] == "no_heartbeat"
        finally:
            svc.stop()

    def test_household_agents_with_fresh_heartbeat(self, daemon_home):
        from datetime import datetime, timezone
        agent_dir = daemon_home / "agents" / "alivebot"
        (agent_dir / "identity").mkdir(parents=True)
        (agent_dir / "identity" / "identity.json").write_text(
            json.dumps({"name": "Alivebot"})
        )
        hb_dir = daemon_home / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        (hb_dir / "alivebot.json").write_text(json.dumps({
            "agent_name": "Alivebot",
            "status": "alive",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 300,
        }))
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/household/agents")
            agent = data["agents"][0]
            assert agent["heartbeat"]["alive"] is True
            assert agent["status"] == "alive"
        finally:
            svc.stop()

    def test_household_agents_stale_heartbeat(self, daemon_home):
        agent_dir = daemon_home / "agents" / "stalebot"
        (agent_dir / "identity").mkdir(parents=True)
        (agent_dir / "identity" / "identity.json").write_text(json.dumps({"name": "Stalebot"}))
        hb_dir = daemon_home / "heartbeats"
        hb_dir.mkdir(parents=True, exist_ok=True)
        (hb_dir / "stalebot.json").write_text(json.dumps({
            "agent_name": "Stalebot",
            "status": "alive",
            "timestamp": "2020-01-01T00:00:00+00:00",
            "ttl_seconds": 300,
        }))
        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/household/agents")
            agent = data["agents"][0]
            assert agent["heartbeat"]["alive"] is False
            assert agent["status"] == "stale"
        finally:
            svc.stop()

    # ── /api/v1/household/agent/{name} ───────────────────────────────────

    def test_single_agent_not_found(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, data = self._get_404(svc.config.port, "/api/v1/household/agent/nobody")
            assert status == 404
            assert "not found" in data["error"]
        finally:
            svc.stop()

    def test_single_agent_detail(self, daemon_home):
        agent_dir = daemon_home / "agents" / "opus"
        (agent_dir / "identity").mkdir(parents=True)
        (agent_dir / "identity" / "identity.json").write_text(
            json.dumps({"name": "Opus", "fingerprint": "DEADBEEF"})
        )
        mem_dir = agent_dir / "memory" / "short-term"
        mem_dir.mkdir(parents=True)
        (mem_dir / "mem1.json").write_text("{}")
        (mem_dir / "mem2.json").write_text("{}")

        svc = self._start_server(daemon_home)
        try:
            status, data = self._get(svc.config.port, "/api/v1/household/agent/opus")
            assert status == 200
            assert data["name"] == "opus"
            assert data["identity"]["fingerprint"] == "DEADBEEF"
            assert data["memory_count"] == 2
            assert "recent_conversations" in data
        finally:
            svc.stop()

    # ── /api/v1/conversations ────────────────────────────────────────────

    def test_conversations_empty(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, data = self._get(svc.config.port, "/api/v1/conversations")
            assert status == 200
            assert data["conversations"] == []
        finally:
            svc.stop()

    def test_conversations_list(self, daemon_home):
        conv_dir = daemon_home / "conversations"
        conv_dir.mkdir(parents=True)
        msgs = [
            {"role": "user", "content": "hi", "timestamp": "2026-03-01T10:00:00+00:00"},
            {"role": "assistant", "content": "hello", "timestamp": "2026-03-01T10:00:01+00:00"},
        ]
        (conv_dir / "alice.json").write_text(json.dumps(msgs))

        svc = self._start_server(daemon_home)
        try:
            _, data = self._get(svc.config.port, "/api/v1/conversations")
            assert len(data["conversations"]) == 1
            c = data["conversations"][0]
            assert c["peer"] == "alice"
            assert c["message_count"] == 2
            assert c["last_message"] == "2026-03-01T10:00:01+00:00"
        finally:
            svc.stop()

    # ── /api/v1/conversations/{peer} ─────────────────────────────────────

    def test_conversation_peer_not_found(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, data = self._get_404(svc.config.port, "/api/v1/conversations/nobody")
            assert status == 404
            assert "nobody" in data["error"]
        finally:
            svc.stop()

    def test_conversation_peer_history(self, daemon_home):
        conv_dir = daemon_home / "conversations"
        conv_dir.mkdir(parents=True)
        msgs = [
            {"role": "user", "content": "ping", "timestamp": "2026-03-01T09:00:00+00:00"},
        ]
        (conv_dir / "bob.json").write_text(json.dumps(msgs))

        svc = self._start_server(daemon_home)
        try:
            status, data = self._get(svc.config.port, "/api/v1/conversations/bob")
            assert status == 200
            assert data["peer"] == "bob"
            assert len(data["messages"]) == 1
            assert data["messages"][0]["content"] == "ping"
        finally:
            svc.stop()


class TestDashboardAPI:
    """Tests for GET / (HTML dashboard) and GET /api/v1/dashboard (JSON)."""

    def _start_server(self, daemon_home, shared_root=None):
        root = shared_root or daemon_home
        config = DaemonConfig(
            home=daemon_home, shared_root=root, port=0, poll_interval=60
        )
        svc = DaemonService(config)
        svc.state.running = True
        with patch.object(svc, "_load_components"):
            svc.config.port = _find_free_port()
            svc._write_pid()
            svc._start_api_server()
        time.sleep(0.3)
        return svc

    def _get(self, port, path):
        url = f"http://127.0.0.1:{port}{path}"
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "")

    # ── GET /api/v1/dashboard ────────────────────────────────────────────

    def test_dashboard_json_returns_200(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, body, ct = self._get(svc.config.port, "/api/v1/dashboard")
            assert status == 200
            assert "application/json" in ct
            data = json.loads(body)
            assert "agent" in data
            assert "daemon" in data
            assert "consciousness" in data
            assert "backends" in data
            assert "conversations" in data
            assert "system" in data
            assert "recent_errors" in data
        finally:
            svc.stop()

    def test_dashboard_json_daemon_fields(self, daemon_home):
        svc = self._start_server(daemon_home)
        svc.state.record_poll(7)
        try:
            _, body, _ = self._get(svc.config.port, "/api/v1/dashboard")
            data = json.loads(body)
            assert data["daemon"]["running"] is True
            assert data["daemon"]["messages_received"] == 7
            assert "uptime_seconds" in data["daemon"]
            assert "pid" in data["daemon"]
        finally:
            svc.stop()

    def test_dashboard_json_system_stats(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/api/v1/dashboard")
            data = json.loads(body)
            sys_stats = data["system"]
            assert "disk_total_gb" in sys_stats
            assert "memory_total_mb" in sys_stats
            assert sys_stats["disk_total_gb"] > 0
        finally:
            svc.stop()

    def test_dashboard_json_conversations_last5(self, daemon_home):
        conv_dir = daemon_home / "conversations"
        conv_dir.mkdir(parents=True)
        for i in range(7):
            msgs = [{"role": "user", "content": f"msg{i}", "timestamp": f"2026-03-0{i % 9 + 1}T10:00:00+00:00"}]
            (conv_dir / f"peer{i}.json").write_text(json.dumps(msgs))

        svc = self._start_server(daemon_home, shared_root=daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/api/v1/dashboard")
            data = json.loads(body)
            assert len(data["conversations"]) <= 5
        finally:
            svc.stop()

    def test_dashboard_json_identity_from_file(self, daemon_home):
        (daemon_home / "identity").mkdir(parents=True, exist_ok=True)
        (daemon_home / "identity" / "identity.json").write_text(
            json.dumps({"name": "TestAgent", "fingerprint": "DEADBEEF12345678"})
        )
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/api/v1/dashboard")
            data = json.loads(body)
            assert data["agent"]["name"] == "TestAgent"
            assert data["agent"]["fingerprint"] == "DEADBEEF12345678"
        finally:
            svc.stop()

    # ── GET / (HTML dashboard) ───────────────────────────────────────────

    def test_dashboard_html_returns_200(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, body, ct = self._get(svc.config.port, "/")
            assert status == 200
            assert "text/html" in ct
            html = body.decode("utf-8")
            assert "<!DOCTYPE html>" in html
        finally:
            svc.stop()

    def test_dashboard_html_dark_theme(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/")
            html = body.decode("utf-8")
            assert "#0d1117" in html  # GitHub dark background
        finally:
            svc.stop()

    def test_dashboard_html_auto_refresh(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/")
            html = body.decode("utf-8")
            assert 'http-equiv="refresh"' in html
            assert "content=\"30\"" in html
        finally:
            svc.stop()

    def test_dashboard_html_contains_agent_section(self, daemon_home):
        (daemon_home / "identity").mkdir(parents=True, exist_ok=True)
        (daemon_home / "identity" / "identity.json").write_text(
            json.dumps({"name": "Opus", "fingerprint": "ABCD1234ABCD1234ABCD1234"})
        )
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/")
            html = body.decode("utf-8")
            assert "Opus" in html
            assert "Daemon" in html
            assert "Consciousness" in html
            assert "Backends" in html
            assert "System" in html
        finally:
            svc.stop()

    def test_dashboard_html_dot_indicators(self, daemon_home):
        """Verify green/red dot CSS classes are present."""
        svc = self._start_server(daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/")
            html = body.decode("utf-8")
            assert "dot-green" in html
            assert "dot-red" in html
        finally:
            svc.stop()

    def test_dashboard_html_shows_conversation_peers(self, daemon_home):
        conv_dir = daemon_home / "conversations"
        conv_dir.mkdir(parents=True)
        msgs = [{"role": "user", "content": "hi", "timestamp": "2026-03-01T10:00:00+00:00"}]
        (conv_dir / "alice.json").write_text(json.dumps(msgs))

        svc = self._start_server(daemon_home, shared_root=daemon_home)
        try:
            _, body, _ = self._get(svc.config.port, "/")
            html = body.decode("utf-8")
            assert "alice" in html
        finally:
            svc.stop()


class TestCORSHeaders:
    """Tests for CORS headers on all API responses (Flutter web access)."""

    def _start_server(self, daemon_home):
        config = DaemonConfig(home=daemon_home, shared_root=daemon_home, port=0, poll_interval=60)
        svc = DaemonService(config)
        svc.state.running = True
        with patch.object(svc, "_load_components"):
            svc.config.port = _find_free_port()
            svc._write_pid()
            svc._start_api_server()
        time.sleep(0.3)
        return svc

    def _request(self, port, path, method="GET"):
        import urllib.error
        url = f"http://127.0.0.1:{port}{path}"
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status, resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers

    def test_options_preflight_returns_204(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, _ = self._request(svc.config.port, "/ping", method="OPTIONS")
            assert status == 204
        finally:
            svc.stop()

    def test_options_preflight_cors_headers(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            _, headers = self._request(svc.config.port, "/api/v1/status", method="OPTIONS")
            assert headers.get("Access-Control-Allow-Origin") == "*"
            allow_methods = headers.get("Access-Control-Allow-Methods", "")
            assert "GET" in allow_methods
            assert "POST" in allow_methods
            assert "OPTIONS" in allow_methods
            assert "Content-Type" in headers.get("Access-Control-Allow-Headers", "")
        finally:
            svc.stop()

    def test_get_response_has_cors_origin(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, headers = self._request(svc.config.port, "/ping")
            assert status == 200
            assert headers.get("Access-Control-Allow-Origin") == "*"
        finally:
            svc.stop()

    def test_json_response_has_cors_headers(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, headers = self._request(svc.config.port, "/api/v1/conversations")
            assert status == 200
            assert headers.get("Access-Control-Allow-Origin") == "*"
            assert headers.get("Access-Control-Allow-Methods") is not None
        finally:
            svc.stop()

    def test_html_response_has_cors_headers(self, daemon_home):
        svc = self._start_server(daemon_home)
        try:
            status, headers = self._request(svc.config.port, "/")
            assert status == 200
            assert headers.get("Access-Control-Allow-Origin") == "*"
        finally:
            svc.stop()


def _find_free_port() -> int:
    """Find an available port for testing."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
