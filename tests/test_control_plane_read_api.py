from __future__ import annotations

import base64
import bisect
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from skcoord.card import Card, Column, Kind
from skcoord.card_store import CardStore
from skcoord.coordination import Board, TaskViewReadBatch, TaskViewReadScope
from starlette.testclient import TestClient

from skdashboard.control_plane_api import _capauth_authorize
from skdashboard.dashboard import create_app

LAN_ORIGIN = "https://10.0.0.139:7778"
TAILNET_ORIGIN = "https://100.81.238.58:7778"
READ_HEADERS = {"Authorization": "Bearer valid-read", "Origin": LAN_ORIGIN}
EVENT_HEADERS = {"Authorization": "Bearer valid-events", "Origin": TAILNET_ORIGIN}
OIDC_FINGERPRINT = "a" * 40


def _authorizer(bearer: str, capability: str, _target: str) -> bool:
    return (bearer, capability) in {
        ("valid-read", "skdashboard.read"),
        ("valid-events", "skdashboard.events.read"),
    }


def _app(home: Path):
    return create_app(home, control_plane_authorizer=_authorizer)


def _canonical_bearer(scope: str, *, subject: str = OIDC_FINGERPRINT) -> str:
    from capauth.tokens import SignedToken, TokenPayload, TokenType, export_token

    issued = datetime.now(timezone.utc)
    token = SignedToken(
        payload=TokenPayload(
            token_id="a" * 64,
            token_type=TokenType.CAPABILITY,
            issuer="B" * 40,
            subject=subject,
            capabilities=[scope],
            issued_at=issued,
            expires_at=issued + timedelta(minutes=1),
            audience="skdashboard",
        ),
        signature="test-signature",
    )
    return base64.urlsafe_b64encode(export_token(token).encode()).decode("ascii")


def _bounded_board_reader(counter: dict[str, int]):
    card_ids = tuple(f"synthetic-{index:05d}" for index in range(10_000))
    population = hashlib.sha256(json.dumps(card_ids).encode()).hexdigest()

    def read(home: Path, *, limit: int, cursor: str | None = None) -> dict:
        def owner_page(after: str | None, count: int) -> TaskViewReadBatch:
            counter["calls"] += 1
            start = 0 if after is None else bisect.bisect_right(card_ids, after)
            selected = card_ids[start : start + count]
            counter["records"] += len(selected)
            return TaskViewReadBatch(selected, population)

        scope = TaskViewReadScope("public-synthetic:board", owner_page)
        page = Board(home).get_task_view_page(scope, limit=limit, cursor=cursor)
        return {
            "tasks": [
                {
                    "id": view.task.id,
                    "title": view.task.title,
                    "priority": view.task.priority.value,
                    "status": view.status.value,
                    "claimed_by": view.claimed_by,
                }
                for view in page.items
            ],
            "page": {
                "limit": limit,
                "next_cursor": page.next_cursor,
                "has_more": page.has_more,
            },
        }

    return read


def _bounded_app(home: Path, counter: dict[str, int]):
    return create_app(
        home,
        control_plane_authorizer=_authorizer,
        control_plane_board_reader=_bounded_board_reader(counter),
    )


def test_v1_board_is_bounded_etagged_and_read_only(tmp_path: Path) -> None:
    board = {
        "tasks": [
            {
                "id": str(i),
                "title": f"Task {i}",
                "priority": "high",
                "status": "open",
                "claimed_by": None,
            }
            for i in range(3)
        ],
        "summary": {"total": 3, "done": 0, "open": 3, "in_progress": 0},
    }
    app = _app(tmp_path)
    with patch("skdashboard.dashboard._get_board_state", return_value=board):
        # The route captures its adapter at app construction, so replace the
        # read method at its source and prove no Board mutation API is touched.
        pass
    with (
        patch("skcoord.coordination.Board.get_task_views") as reads,
        patch("skcoord.coordination.Board.load_agents", return_value=[]),
        patch(
            "skcoord.coordination.Board.claim", side_effect=AssertionError("mutation"), create=True
        ),
    ):
        reads.return_value = []
        response = TestClient(app).get("/api/v1/board/summary?limit=1", headers=READ_HEADERS)
    assert response.status_code == 200
    assert response.json()["schema_version"] == "1.1.0"
    assert response.json()["freshness"]["truth_state"] == "unknown"
    assert response.headers["etag"]
    assert (
        TestClient(app).get("/api/v1/board/summary?limit=201", headers=READ_HEADERS).status_code
        == 400
    )


def test_limit_one_touches_only_page_plus_cursor_record(tmp_path: Path) -> None:
    counter = {"calls": 0, "records": 0}

    def fold(_store, card_id):
        return Card(
            id=card_id,
            kind=Kind.TASK,
            title=card_id,
            status=Column.BACKLOG,
            swimlane="feature",
            source="cards",
        )

    with patch.object(CardStore, "fold", fold):
        response = TestClient(_bounded_app(tmp_path, counter)).get(
            "/api/v1/board/summary?limit=1", headers=READ_HEADERS
        )

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["page"]["has_more"] is True
    assert counter == {"calls": 1, "records": 2}


def test_production_board_reader_defers_folds_to_page_plus_one(tmp_path: Path) -> None:
    from skdashboard.dashboard import _BoundedBoardReader

    card_ids = tuple(f"{index:08x}" for index in range(10_000))
    folds: list[str] = []

    def fold(_store, card_id):
        folds.append(card_id)
        return Card(
            id=card_id,
            kind=Kind.TASK,
            title=card_id,
            status=Column.BACKLOG,
            swimlane="feature",
            source="cards",
        )

    with patch.object(CardStore, "list_card_ids", return_value=list(card_ids)), patch.object(
        CardStore, "fold", fold
    ), patch.object(
        CardStore,
        "list_cards",
        side_effect=AssertionError("full CardStore fold is forbidden"),
    ):
        reader = _BoundedBoardReader(tmp_path)
        result = reader(tmp_path, limit=1)

    assert len(result["tasks"]) == 1
    assert result["eligible_records_touched"] == 2
    assert folds == ["00000000", "00000001"]


def test_matching_etag_avoids_owner_recomputation(tmp_path: Path) -> None:
    counter = {"calls": 0, "records": 0}

    def fold(_store, card_id):
        return Card(
            id=card_id,
            kind=Kind.TASK,
            title=card_id,
            status=Column.BACKLOG,
            swimlane="feature",
            source="cards",
        )

    with patch.object(CardStore, "fold", fold):
        client = TestClient(_bounded_app(tmp_path, counter))
        first = client.get("/api/v1/board/summary?limit=1", headers=READ_HEADERS)
        unchanged = client.get(
            "/api/v1/board/summary?limit=1",
            headers={**READ_HEADERS, "If-None-Match": first.headers["etag"]},
        )

    assert first.status_code == 200
    assert unchanged.status_code == 304
    assert counter == {"calls": 1, "records": 2}


def test_common_read_saturation_cannot_rate_limit_health(tmp_path: Path) -> None:
    counter = {"calls": 0, "records": 0}

    def board_reader(_home: Path, *, limit: int, cursor: str | None = None) -> dict:
        del cursor
        counter["calls"] += 1
        return {"tasks": [], "page": {"limit": limit, "next_cursor": None, "has_more": False}}

    app = create_app(
        tmp_path,
        control_plane_authorizer=_authorizer,
        control_plane_board_reader=board_reader,
    )
    client = TestClient(app)
    for _ in range(120):
        assert client.get("/api/v1/board/summary?limit=1", headers=READ_HEADERS).status_code == 200

    assert client.get("/api/v1/health").status_code == 200


def test_v1_source_failure_is_partial_not_healthy(tmp_path: Path) -> None:
    app = create_app(
        tmp_path,
        control_plane_authorizer=_authorizer,
        control_plane_board_reader=lambda _home, **_query: {"error": "offline"},
    )
    response = TestClient(app).get("/api/v1/board/summary", headers=READ_HEADERS)
    body = response.json()
    assert body["freshness"]["truth_state"] == "partial"
    assert body["errors"][0]["code"] == "SOURCE_PARTIAL"


def test_v1_economy_keeps_unavailable_cost_distinct_from_zero(tmp_path: Path) -> None:
    usage = {
        "generated_at": "2026-08-23T00:00:00Z",
        "selected_lane": "harness_reported",
        "available_lanes": ["harness_reported"],
        "summary": {
            "input": 2,
            "output": 3,
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total": 5,
            "cost_usd": 0.0,
            "cost_state": "unavailable",
        },
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
    with patch(
        "skdashboard.dashboard_fleet.get_drift",
        return_value={"nodes": [], "skipped": [], "errors": []},
    ) as read:
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
    assert stream.status_code == 403
    assert stream.json()["message"] == "typed Tenant and caller context is required"
    assert (
        client.get(
            "/api/v1/events?topics=" + ",".join(["x"] * 17), headers=EVENT_HEADERS
        ).status_code
        == 403
    )


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
    assert client.get("/api/v1/events?cursor=djE6MQ", headers=EVENT_HEADERS).status_code == 403
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


def test_canonical_capauth_transport_reaches_signature_and_pdp_for_all_routes(
    tmp_path: Path,
) -> None:
    read = _canonical_bearer("skdashboard.read")
    events = _canonical_bearer("skdashboard.events.read")
    with (
        patch("capauth.tokens.verify_audience_token", return_value=True) as verify,
        patch("capauth.authz.decide", return_value=SimpleNamespace(allow=True)) as decide,
    ):
        client = TestClient(create_app(tmp_path))
        for path in ("/api/v1/overview", "/metrics"):
            response = client.get(
                path,
                headers={"Authorization": f"Bearer {read}", "Origin": LAN_ORIGIN},
            )
            assert response.status_code == 200
        response = client.get(
            "/api/v1/events?cursor=djE6MQ",
            headers={"Authorization": f"Bearer {events}", "Origin": TAILNET_ORIGIN},
        )
    assert response.status_code == 403
    assert response.json()["message"] == "typed Tenant and caller context is required"
    assert verify.call_count == 2
    assert decide.call_count == 2


def test_oidc_fingerprint_is_canonicalized_before_policy_decision(tmp_path: Path) -> None:
    bearer = _canonical_bearer("skdashboard.read")

    def allow_canonical_device(subject, *_args, **_kwargs):
        return SimpleNamespace(allow=subject == f"device:{OIDC_FINGERPRINT}")

    with patch("capauth.tokens.verify_audience_token", return_value=True), patch(
        "capauth.authz.decide", side_effect=allow_canonical_device
    ) as decide:
        assert _capauth_authorize(
            tmp_path, bearer, "skdashboard.read", "/api/v1/overview"
        )
    assert decide.call_args.args[0] == f"device:{OIDC_FINGERPRINT}"


def test_non_fingerprint_oidc_subject_is_denied_before_policy_decision(
    tmp_path: Path,
) -> None:
    malformed = ("human@example.test", "a" * 39, "g" * 40, f"device:{OIDC_FINGERPRINT}")
    with patch("capauth.tokens.verify_audience_token", return_value=True) as verify, patch(
        "capauth.authz.decide", return_value=SimpleNamespace(allow=True)
    ) as decide:
        for subject in malformed:
            assert not _capauth_authorize(
                tmp_path,
                _canonical_bearer("skdashboard.read", subject=subject),
                "skdashboard.read",
                "/api/v1/overview",
            )
    assert verify.call_count == len(malformed)
    decide.assert_not_called()


def test_noncanonical_capauth_transport_is_denied_before_crypto(tmp_path: Path) -> None:
    canonical = _canonical_bearer("skdashboard.read")
    decoded = base64.urlsafe_b64decode(canonical).decode()
    unsafe = (decoded, canonical.rstrip("="), canonical + "\n", "%%%%")
    with (
        patch("capauth.tokens.verify_audience_token", return_value=True) as verify,
        patch("capauth.authz.decide", return_value=SimpleNamespace(allow=True)) as decide,
    ):
        for bearer in unsafe:
            assert not _capauth_authorize(tmp_path, bearer, "skdashboard.read", "/api/v1/overview")
    verify.assert_not_called()
    decide.assert_not_called()
