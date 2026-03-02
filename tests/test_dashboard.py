"""Tests for the skcapstone web dashboard.

Covers:
- DashboardHandler serves HTML at /
- API endpoints return valid JSON (/api/status, /api/doctor, /api/board, /api/memory)
- /api/daemon returns Flutter-ready JSON with all required fields
- _get_daemon_json returns correct structure when daemon is offline
- skcapstone dashboard --json emits valid JSON to stdout
- 404 for unknown paths
- start_dashboard creates a server
- Dashboard HTML contains essential elements
"""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from skcapstone.dashboard import (
    DashboardHandler,
    _DASHBOARD_HTML,
    _get_agent_status,
    _get_board_state,
    _get_doctor_report,
    _get_daemon_json,
    _get_memory_stats,
    start_dashboard,
)


@pytest.fixture
def agent_home(tmp_path):
    """Create a minimal agent home for dashboard testing."""
    home = tmp_path / ".skcapstone"
    for d in ["identity", "memory", "memory/short-term", "memory/mid-term",
              "memory/long-term", "trust", "security", "sync", "sync/outbox",
              "sync/inbox", "config", "coordination", "coordination/tasks",
              "coordination/agents"]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "DashBot", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "DashBot", "fingerprint": "DASH1234", "capauth_managed": False,
    }))
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "DashBot"}))
    (home / "memory" / "index.json").write_text("{}")
    (home / "memory" / "short-term" / "m1.json").write_text(
        json.dumps({"memory_id": "m1", "content": "test", "tags": [],
                     "source": "test", "importance": 0.5, "layer": "short-term",
                     "created_at": "2026-02-24T00:00:00Z", "access_count": 0,
                     "accessed_at": None, "metadata": {}})
    )

    return home


@pytest.fixture
def dashboard_server(agent_home):
    """Start a dashboard server on a random-ish port for testing."""
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    server = start_dashboard(agent_home, port=port)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)

    yield server, port

    server.shutdown()


class TestDashboardHTML:
    """Test the embedded HTML dashboard."""

    def test_html_contains_title(self):
        """HTML has the dashboard title."""
        assert "SKCapstone" in _DASHBOARD_HTML
        assert "Dashboard" in _DASHBOARD_HTML

    def test_html_contains_api_calls(self):
        """HTML fetches all API endpoints."""
        assert "/api/status" in _DASHBOARD_HTML
        assert "/api/doctor" in _DASHBOARD_HTML
        assert "/api/board" in _DASHBOARD_HTML
        assert "/api/memory" in _DASHBOARD_HTML

    def test_html_auto_refreshes(self):
        """HTML has auto-refresh interval."""
        assert "setInterval" in _DASHBOARD_HTML


class TestAPIFunctions:
    """Test the data-fetching functions directly."""

    def test_get_agent_status(self, agent_home):
        """Agent status returns dict with pillar info."""
        status = _get_agent_status(agent_home)

        assert "name" in status
        assert "pillars" in status
        assert "identity" in status
        assert "memory" in status

    def test_get_doctor_report(self, agent_home):
        """Doctor report returns dict with checks."""
        report = _get_doctor_report(agent_home)

        assert "checks" in report
        assert "passed" in report
        assert "total" in report
        assert report["total"] > 0

    def test_get_board_state(self, agent_home):
        """Board state returns tasks and agents."""
        board = _get_board_state(agent_home)

        assert "tasks" in board
        assert "agents" in board
        assert "summary" in board

    def test_get_memory_stats(self, agent_home):
        """Memory stats returns layer counts."""
        stats = _get_memory_stats(agent_home)

        assert "total" in stats
        assert "short_term" in stats
        assert "mid_term" in stats
        assert "long_term" in stats

    def test_status_handles_missing_home(self, tmp_path):
        """Status returns error dict for missing agent home."""
        result = _get_agent_status(tmp_path / "nope")
        assert "error" in result or "name" in result


class TestHTTPServer:
    """Test the actual HTTP server endpoints."""

    def test_serves_html_at_root(self, dashboard_server):
        """GET / returns the HTML dashboard."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()

        assert resp.status == 200
        assert "text/html" in resp.getheader("Content-Type")
        body = resp.read().decode("utf-8")
        assert "SKCapstone" in body
        conn.close()

    def test_api_status_json(self, dashboard_server):
        """GET /api/status returns valid JSON."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()

        assert resp.status == 200
        assert "application/json" in resp.getheader("Content-Type")
        data = json.loads(resp.read())
        assert "name" in data or "error" in data
        conn.close()

    def test_api_doctor_json(self, dashboard_server):
        """GET /api/doctor returns valid JSON."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/doctor")
        resp = conn.getresponse()

        assert resp.status == 200
        data = json.loads(resp.read())
        assert "checks" in data or "error" in data
        conn.close()

    def test_api_board_json(self, dashboard_server):
        """GET /api/board returns valid JSON."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/board")
        resp = conn.getresponse()

        assert resp.status == 200
        data = json.loads(resp.read())
        assert "tasks" in data or "error" in data
        conn.close()

    def test_api_memory_json(self, dashboard_server):
        """GET /api/memory returns valid JSON."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/memory")
        resp = conn.getresponse()

        assert resp.status == 200
        data = json.loads(resp.read())
        assert "total" in data or "error" in data
        conn.close()

    def test_404_unknown_path(self, dashboard_server):
        """Unknown paths return 404."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/nonexistent")
        resp = conn.getresponse()

        assert resp.status == 404
        conn.close()

    def test_cors_header(self, dashboard_server):
        """API responses include CORS header."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/status")
        resp = conn.getresponse()

        assert resp.getheader("Access-Control-Allow-Origin") == "*"
        conn.close()


class TestGetDaemonJson:
    """Unit tests for _get_daemon_json() — the Flutter-ready status blob."""

    def test_returns_all_required_keys(self, agent_home):
        """_get_daemon_json always returns all top-level keys even when daemon is offline."""
        result = _get_daemon_json(agent_home, daemon_port=19999)  # unused port

        assert "generated_at" in result
        assert "daemon" in result
        assert "consciousness" in result
        assert "backend_health" in result
        assert "active_conversations" in result
        assert "system" in result

    def test_daemon_offline_returns_safe_defaults(self, agent_home):
        """When daemon is unreachable, daemon section shows running=False with zero counts."""
        result = _get_daemon_json(agent_home, daemon_port=19999)

        daemon = result["daemon"]
        assert daemon["running"] is False
        assert daemon["messages_received"] == 0
        assert daemon["error_count"] == 0
        assert daemon["uptime_seconds"] == 0
        assert isinstance(daemon["recent_errors"], list)

    def test_consciousness_offline_returns_disabled(self, agent_home):
        """When daemon is unreachable, consciousness section shows enabled=False."""
        result = _get_daemon_json(agent_home, daemon_port=19999)

        assert result["consciousness"].get("enabled") is False

    def test_backend_health_has_expected_backends(self, agent_home):
        """backend_health contains at least the standard five backends."""
        result = _get_daemon_json(agent_home, daemon_port=19999)

        bh = result["backend_health"]
        for key in ("ollama", "anthropic", "grok", "kimi", "nvidia"):
            assert key in bh, f"Missing backend key: {key}"
            assert isinstance(bh[key], bool)

    def test_generated_at_is_iso_timestamp(self, agent_home):
        """generated_at is a valid ISO 8601 timestamp string."""
        from datetime import datetime

        result = _get_daemon_json(agent_home, daemon_port=19999)
        ts = result["generated_at"]
        # Should not raise
        datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def test_daemon_running_returns_full_snapshot(self, agent_home):
        """When daemon HTTP returns a valid snapshot, all fields are populated."""
        fake_snap = {
            "running": True,
            "pid": 42,
            "uptime_seconds": 3725,
            "started_at": "2026-03-02T10:00:00+00:00",
            "messages_received": 17,
            "syncs_completed": 3,
            "recent_errors": ["err1"],
            "inflight_count": 2,
        }
        fake_consciousness = {
            "enabled": True,
            "messages_processed": 10,
            "messages_processed_24h": 5,
            "responses_sent": 9,
            "errors": 1,
            "backends": {"ollama": True, "grok": False},
        }

        import urllib.request

        class _FakeResponse:
            def __init__(self, data):
                self._data = json.dumps(data).encode()

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

        call_count = {"n": 0}

        def fake_urlopen(url_or_req, timeout=None):
            url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
            call_count["n"] += 1
            if "/status" in url:
                return _FakeResponse(fake_snap)
            if "/consciousness" in url:
                return _FakeResponse(fake_consciousness)
            raise OSError("not available")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = _get_daemon_json(agent_home, daemon_port=7777)

        daemon = result["daemon"]
        assert daemon["running"] is True
        assert daemon["pid"] == 42
        assert daemon["messages_received"] == 17
        assert daemon["error_count"] == 1
        assert daemon["uptime_human"] == "1h 2m"

        csc = result["consciousness"]
        assert csc["enabled"] is True
        assert csc["messages_processed"] == 10


class TestDaemonApiEndpoint:
    """Test the /api/daemon HTTP endpoint on the dashboard server."""

    def test_api_daemon_json(self, dashboard_server):
        """GET /api/daemon returns valid JSON with all required top-level keys."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/daemon")
        resp = conn.getresponse()

        assert resp.status == 200
        assert "application/json" in resp.getheader("Content-Type")
        data = json.loads(resp.read())
        for key in ("generated_at", "daemon", "consciousness", "backend_health",
                    "active_conversations", "system"):
            assert key in data, f"Missing key in /api/daemon response: {key}"
        conn.close()

    def test_api_daemon_cors_header(self, dashboard_server):
        """/api/daemon includes the CORS header for cross-origin Flutter access."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/daemon")
        resp = conn.getresponse()

        assert resp.getheader("Access-Control-Allow-Origin") == "*"
        conn.close()

    def test_api_daemon_backend_health_keys(self, dashboard_server):
        """/api/daemon backend_health contains all standard backend names."""
        server, port = dashboard_server
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/daemon")
        resp = conn.getresponse()

        data = json.loads(resp.read())
        bh = data.get("backend_health", {})
        for key in ("ollama", "anthropic", "grok", "kimi", "nvidia"):
            assert key in bh
        conn.close()


class TestDashboardJsonCLI:
    """Test the skcapstone dashboard --json CLI flag."""

    def test_json_flag_outputs_valid_json(self, agent_home):
        """dashboard --json prints JSON to stdout and exits without starting a server."""
        from click.testing import CliRunner
        from skcapstone.cli.status import register_status_commands
        import click

        @click.group()
        def _cli():
            pass

        register_status_commands(_cli)
        runner = CliRunner()
        result = runner.invoke(
            _cli,
            ["dashboard", "--json", f"--home={agent_home}"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "daemon" in data
        assert "consciousness" in data
        assert "backend_health" in data

    def test_json_flag_contains_generated_at(self, agent_home):
        """dashboard --json output includes a generated_at timestamp."""
        from click.testing import CliRunner
        from skcapstone.cli.status import register_status_commands
        import click

        @click.group()
        def _cli():
            pass

        register_status_commands(_cli)
        runner = CliRunner()
        result = runner.invoke(
            _cli,
            ["dashboard", "--json", f"--home={agent_home}"],
            catch_exceptions=False,
        )
        data = json.loads(result.output)
        assert "generated_at" in data

    def test_json_flag_daemon_offline_still_exits_zero(self, agent_home):
        """dashboard --json exits 0 even when daemon is unreachable (daemon offline)."""
        from click.testing import CliRunner
        from skcapstone.cli.status import register_status_commands
        import click

        @click.group()
        def _cli():
            pass

        register_status_commands(_cli)
        runner = CliRunner()
        result = runner.invoke(
            _cli,
            ["dashboard", "--json", "--daemon-port=19999", f"--home={agent_home}"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["daemon"]["running"] is False
