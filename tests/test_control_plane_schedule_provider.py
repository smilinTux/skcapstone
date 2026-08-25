from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from starlette.testclient import TestClient
from test_control_plane_decision_context import ORIGIN, Rig

from skdashboard.dashboard import create_app

NOW = datetime.now(timezone.utc)
HASH = "sha256:" + "a" * 64


def _projection(query) -> dict:
    return {
        "schema_version": "1.0.0",
        "projection_id": "schedule-1",
        "projection_version": "projection-v1",
        "projection_hash": HASH,
        "scope": {"role": query["role"], "service_id": "all"},
        "display_timezone": query["timezone"],
        "observed_at": NOW.isoformat(),
        "projected_at": (NOW + timedelta(seconds=1)).isoformat(),
        "truth_state": "current",
        "visibility": {"state": "visible", "authorization": "authorized"},
        "source_watermarks": [{"source": "fixture", "value": "fixture-v1"}],
        "items": [],
        "dependencies": [],
        "overlays": [],
        "cycle_analysis": {"state": "acyclic", "cycle_item_ids": [], "evidence_refs": []},
        "critical_path": {
            "state": "not_applicable",
            "item_ids": [],
            "reasons": ["not_applicable"],
        },
        "individual_ranking_prohibited": True,
        "errors": [],
    }


def test_schedule_provider_receives_exact_context_scope_and_verifier(tmp_path: Path) -> None:
    rig = Rig(target="/api/v1/schedule/projection")
    calls = []

    class Provider:
        def read(self, context, query, home, *, currentness_verifier):
            assert context.binding == rig.binding
            assert home == tmp_path
            assert currentness_verifier.check_before_owner_read(context).value == "allow"
            calls.append(dict(query))
            assert currentness_verifier.check_after_owner_read(context).value == "allow"
            return _projection(query)

    app = create_app(
        tmp_path,
        control_plane_decision_authorizer=rig.authorizer,
        control_plane_invocation_factory=rig.factory,
        control_plane_schedule_provider=Provider(),
    )
    path = "/api/v1/schedule/projection?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=gantt&timezone=UTC"
    response = TestClient(app).get(
        path,
        headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["projection_version"] == "projection-v1"
    assert calls == [
        {
            "role": "project-manager",
            "scope": "estate",
            "window": "latest",
            "baseline": "none",
            "service": "all",
            "lens": "gantt",
            "timezone": "UTC",
        }
    ]
    assert response.headers["etag"]


def test_schedule_scope_rejects_unknown_duplicate_and_protected_values(tmp_path: Path) -> None:
    class Provider:
        def read(self, *_args, **_kwargs):
            raise AssertionError("provider must not run")

    base = "/api/v1/schedule/projection?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=roadmap&timezone=UTC"
    for suffix in ("&unknown=value", "&lens=gantt", "&tenant_id=protected"):
        rig = Rig(target="/api/v1/schedule/projection")
        app = create_app(
            tmp_path,
            control_plane_decision_authorizer=rig.authorizer,
            control_plane_invocation_factory=rig.factory,
            control_plane_schedule_provider=Provider(),
        )
        response = TestClient(app).get(
            base + suffix,
            headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_SCHEDULE_SCOPE"


def test_schedule_forecast_provider_is_protected_and_fails_closed(tmp_path: Path) -> None:
    rig = Rig(target="/api/v1/schedule/forecasts")

    class Provider:
        def read(self, context, query, home, *, currentness_verifier):
            assert context.binding == rig.binding
            assert home == tmp_path
            return {"state": "abstained", "writes_owner_records": False}

    app = create_app(tmp_path, control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_schedule_forecast_provider=Provider())
    path = "/api/v1/schedule/forecasts?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=gantt&timezone=UTC"
    response = TestClient(app).get(path, headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN})
    assert response.status_code == 200
    assert response.json()["state"] == "abstained"

    unavailable = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: True)).get(path, headers={"Authorization": "Bearer legacy"})
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "SCHEDULE_FORECAST_UNAVAILABLE"


def test_schedule_forecast_rejects_unsafe_or_unavailable_provider_output(tmp_path: Path) -> None:
    rig = Rig(target="/api/v1/schedule/forecasts")
    path = "/api/v1/schedule/forecasts?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=gantt&timezone=UTC"

    class Unsafe:
        def read(self, *_args, **_kwargs):
            return {"action": "reschedule", "writes_owner_records": True}

    app = create_app(tmp_path, control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_schedule_forecast_provider=Unsafe())
    response = TestClient(app).get(path, headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN})
    assert response.status_code == 503
    assert response.json()["code"] == "SCHEDULE_FORECAST_UNAVAILABLE"

    class Bypass:
        def read(self, *_args, **_kwargs):
            return {"mutation": True, "reschedule": {"owner_operation": "move_date"}, "writes_owner_records": False}

    app = create_app(tmp_path, control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_schedule_forecast_provider=Bypass())
    response = TestClient(app).get(path, headers={"Authorization": f"Bearer {rig.fresh_bearer()}", "Origin": ORIGIN})
    assert response.status_code == 503
