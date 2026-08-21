"""Regression tests for Jarvis daemon routing and ITIL shadow-card sync."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from skcapstone import agent_run
from skcoord.card_store import CardStore
from skcoord.itil import ITILManager

from skdashboard.dashboard import _gateway_admin_base_url, _get_daemon_json, create_app
from skdashboard.dashboard_kanban import get_card, get_kanban


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_daemon_route_uses_configured_base_url(tmp_path, monkeypatch):
    requested = []

    def fake_urlopen(request, timeout=0):
        url = request if isinstance(request, str) else request.full_url
        requested.append((url, timeout))
        if url.endswith("/status"):
            return _Response({"running": True, "pid": 9391, "uptime_seconds": 61})
        if url.endswith("/consciousness"):
            return _Response({"enabled": True, "messages_processed": 3})
        return _Response({"models": []})

    monkeypatch.setenv("SKCAPSTONE_DAEMON_URL", "http://127.0.0.1:9391/")
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.invalid")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = _get_daemon_json(tmp_path)

    assert result["daemon"]["running"] is True
    assert result["consciousness"]["enabled"] is True
    assert ("http://127.0.0.1:9391/status", 3) in requested
    assert ("http://127.0.0.1:9391/consciousness", 3) in requested


def test_explicit_daemon_port_overrides_environment(tmp_path, monkeypatch):
    requested = []

    def fake_urlopen(request, timeout=0):
        url = request if isinstance(request, str) else request.full_url
        requested.append(url)
        return _Response({})

    monkeypatch.setenv("SKCAPSTONE_DAEMON_URL", "http://127.0.0.1:9391")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    _get_daemon_json(tmp_path, daemon_port=9555)

    assert "http://127.0.0.1:9555/status" in requested
    assert "http://127.0.0.1:9555/consciousness" in requested


def test_gateway_admin_url_strips_openai_v1_suffix(monkeypatch):
    monkeypatch.delenv("SKGATEWAY_ADMIN_URL", raising=False)
    monkeypatch.setenv("SKGATEWAY_URL", "http://127.0.0.1:18780/v1/")
    assert _gateway_admin_base_url() == "http://127.0.0.1:18780"


def test_gateway_admin_url_honors_explicit_override(monkeypatch):
    monkeypatch.setenv("SKGATEWAY_URL", "http://127.0.0.1:18780/v1")
    monkeypatch.setenv("SKGATEWAY_ADMIN_URL", "http://gateway-admin.internal:19000/")
    assert _gateway_admin_base_url() == "http://gateway-admin.internal:19000"


def test_models_route_calls_root_admin_endpoint(tmp_path, monkeypatch):
    requested = []

    def fake_urlopen(request, timeout=0):
        url = request if isinstance(request, str) else request.full_url
        requested.append(url)
        return _Response({"object": "list", "data": [{"id": "sk-default", "advertised": True}]})

    monkeypatch.delenv("SKGATEWAY_ADMIN_URL", raising=False)
    monkeypatch.setenv("SKGATEWAY_URL", "http://127.0.0.1:18780/v1")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    app = create_app(tmp_path)
    handler = next(
        route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/models"
    )

    response = asyncio.run(handler(None))

    assert response.status_code == 200
    assert json.loads(response.body)["data"][0]["id"] == "sk-default"
    assert requested == ["http://127.0.0.1:18780/admin/models"]


def _closed_incident_with_queued_run(home: Path) -> str:
    mgr = ITILManager(home)
    inc = mgr.create_incident(
        title="Qdrant down",
        severity="sev3",
        source="health",
        managed_by="jarvis",
    )
    result = agent_run.request_run(
        home,
        inc.id,
        "Investigate the root cause",
        agent="jarvis",
        requester="operator",
    )
    assert result["state"] == "queued"
    mgr.update_incident(
        inc.id,
        agent="jarvis",
        new_status="resolved",
        resolution_summary="Qdrant healthy",
    )
    mgr.update_incident(inc.id, agent="jarvis", new_status="closed")
    return inc.id


def test_closed_itil_incident_moves_done_and_cancels_queued_run(tmp_path):
    incident_id = _closed_incident_with_queued_run(tmp_path)

    board = get_kanban(tmp_path)
    cards = [
        card
        for lane in board["lanes"]
        for column_cards in lane["columns"].values()
        for card in column_cards
    ]
    card = next(card for card in cards if card["id"] == incident_id)

    assert card["status"] == "done"
    assert card["ai"] == "canceled"

    detail = get_card(tmp_path, incident_id)["card"]
    assert detail["meta"]["itil_status"] == "closed"
    assert detail["meta"]["agent_run"]["state"] == "canceled"

    # Repeated reads are idempotent: no duplicate cancellation events.
    get_kanban(tmp_path)
    cancel_events = [
        event
        for event in CardStore(tmp_path)._read_events(incident_id)
        if event.get("action") == "agent_run_state" and event.get("state") == "canceled"
    ]
    assert len(cancel_events) == 1


class _Headers(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class _Request:
    def __init__(self, card_id):
        self.headers = _Headers()
        self.path_params = {"card_id": card_id}
        self.query_params = {}

    async def json(self):
        return {"instruction": "Investigate", "agent": "jarvis", "mode": "propose"}


def test_dashboard_refuses_new_run_for_closed_itil_record(tmp_path, monkeypatch):
    incident_id = _closed_incident_with_queued_run(tmp_path)
    called = []
    monkeypatch.setattr(agent_run, "request_run", lambda *a, **k: called.append((a, k)))
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)
    app = create_app(tmp_path)
    handler = next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == "/api/card/{card_id}/queue-ai"
    )

    response = asyncio.run(handler(_Request(incident_id)))

    assert response.status_code == 409
    assert "closed" in json.loads(response.body)["error"]
    assert called == []
