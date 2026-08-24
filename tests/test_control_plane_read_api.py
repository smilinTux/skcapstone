from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from skdashboard.dashboard import create_app

LAN_ORIGIN = "http://10.0.0.139:7778"
TAILNET_ORIGIN = "http://100.81.238.58:7778"
READ_HEADERS = {"Authorization": "Bearer valid-read", "Origin": LAN_ORIGIN}
EVENT_HEADERS = {"Authorization": "Bearer valid-events", "Origin": TAILNET_ORIGIN}


def _authorizer(bearer: str, capability: str, _target: str) -> bool:
    return (bearer, capability) in {
        ("valid-read", "skdashboard.read"),
        ("valid-events", "skdashboard.events.read"),
    }


def _app(home: Path):
    return create_app(home, control_plane_authorizer=_authorizer)


def test_v1_board_is_bounded_etagged_and_read_only(tmp_path: Path) -> None:
    board = {
        "tasks": [
            {"id": str(i), "title": f"Task {i}", "priority": "high", "status": "open", "claimed_by": None}
            for i in range(3)
        ],
        "summary": {"total": 3, "done": 0, "open": 3, "in_progress": 0},
    }
    app = _app(tmp_path)
    with patch("skdashboard.dashboard._get_board_state", return_value=board):
        # The route captures its adapter at app construction, so replace the
        # read method at its source and prove no Board mutation API is touched.
        pass
    with patch("skcoord.coordination.Board.get_task_views") as reads, patch(
        "skcoord.coordination.Board.load_agents", return_value=[]
    ), patch("skcoord.coordination.Board.claim", side_effect=AssertionError("mutation"), create=True):
        reads.return_value = []
        response = TestClient(app).get("/api/v1/board/summary?limit=1", headers=READ_HEADERS)
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1.0"
    assert response.json()["freshness"]["truth_state"] == "unknown"
    assert response.headers["etag"]
    assert TestClient(app).get("/api/v1/board/summary?limit=201", headers=READ_HEADERS).status_code == 400


def test_v1_source_failure_is_partial_not_healthy(tmp_path: Path) -> None:
    with patch("skdashboard.dashboard._get_board_state", return_value={"error": "offline"}):
        app = _app(tmp_path)
    response = TestClient(app).get("/api/v1/board/summary", headers=READ_HEADERS)
    body = response.json()
    assert body["freshness"]["truth_state"] == "partial"
    assert body["errors"][0]["code"] == "SOURCE_PARTIAL"


def test_v1_economy_keeps_unavailable_cost_distinct_from_zero(tmp_path: Path) -> None:
    usage = {
        "generated_at": "2026-08-23T00:00:00Z",
        "selected_lane": "harness_reported",
        "available_lanes": ["harness_reported"],
        "summary": {"input": 2, "output": 3, "cache_read": 0, "cache_write": 0, "reasoning": 0, "total": 5, "cost_usd": 0.0, "cost_state": "unavailable"},
        "collectors": [],
        "coverage": {"expected_nodes": 1, "reporting_nodes": 0, "missing_nodes": ["node-a"]},
        "errors": [],
    }
    with patch("skdashboard.dashboard_skcounter.get_ai_usage", return_value=usage):
        response = TestClient(_app(tmp_path)).get("/api/v1/economy/summary", headers=READ_HEADERS)
    item = response.json()["items"][0]
    assert item["tokens"]["total"] == 5
    assert item["cost_usd"] is None
    assert item["cost_state"] == "unavailable"


def test_v1_fleet_disables_alert_side_effects(tmp_path: Path) -> None:
    with patch("skdashboard.dashboard_fleet.get_drift", return_value={"nodes": [], "skipped": [], "errors": []}) as read:
        response = TestClient(_app(tmp_path)).get("/api/v1/fleet/summary", headers=READ_HEADERS)
    assert response.status_code == 200
    read.assert_called_once_with(tmp_path, alert=False)


def test_v1_etag_and_sse_reconnect_contract(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    first = client.get("/api/v1/health")
    unchanged = client.get("/api/v1/health", headers={"If-None-Match": first.headers["etag"]})
    assert unchanged.status_code == 304
    cursor = "djE6MQ"
    stream = client.get(f"/api/v1/events?cursor={cursor}", headers=EVENT_HEADERS)
    assert "event: reset-required" in stream.text
    assert ": heartbeat" in stream.text
    assert client.get("/api/v1/events?topics=" + ",".join(["x"] * 17), headers=EVENT_HEADERS).status_code == 400


def test_unknown_and_wrong_method_never_catch_all_success(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    assert client.get("/api/v1/missing").status_code == 404
    assert client.post("/api/v1/health").status_code == 405


def test_v1_rate_control_returns_retry_contract(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    for _ in range(120):
        assert client.get("/api/v1/health").status_code == 200
    response = client.get("/api/v1/health")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "60"
    assert response.json()["code"] == "RATE_LIMITED"


def test_protected_reads_and_equivalent_sse_fail_closed(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    for path in ("/api/v1/overview", "/api/v1/board/summary", "/api/v1/events", "/metrics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 403
        denied = client.get(
            path,
            headers={"Authorization": "Bearer valid-read", "Origin": "https://public.example"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "ORIGIN_DENIED"


def test_named_origins_and_exact_capabilities_preserve_legitimate_reads(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    assert client.get("/api/v1/overview", headers=READ_HEADERS).status_code == 200
    assert client.get("/api/v1/events", headers=EVENT_HEADERS).status_code == 200
    assert client.get("/api/v1/events", headers=READ_HEADERS).status_code == 403


def test_metrics_are_bounded_and_never_echo_sensitive_input(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    secret = "secret-capability-material"
    client.get("/api/v1/overview", headers={"Authorization": f"Bearer {secret}"})
    response = client.get("/metrics", headers=READ_HEADERS)
    assert response.status_code == 200
    assert len(response.content) < 4096
    assert secret not in response.text
    assert str(tmp_path) not in response.text
    assert "prompt" not in response.text.lower()
    assert client.post("/metrics", headers=READ_HEADERS).status_code == 405
