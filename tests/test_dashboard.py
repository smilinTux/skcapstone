"""Tests for the skcapstone web dashboard.

Covers:
- DashboardHandler serves HTML at /
- API endpoints return valid JSON (/api/status, /api/doctor, /api/board, /api/memory)
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
