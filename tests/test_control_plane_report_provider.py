from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient
from test_control_plane_decision_context import ORIGIN, Rig

from skdashboard.dashboard import create_app

PATH = "/api/v1/reports/projection?role=project-manager&scope=estate&window=latest&baseline=none&service=all&report_type=all"


def snapshot():
    return {
        "snapshot_id": "rpt-example-report",
        "schema_version": "1.1.0",
        "report_hash": "sha256:" + "a" * 64,
    }


def projection(query):
    return {
        "schema_version": "1.0.0",
        "projection_id": "reports-latest",
        "scope": query,
        "truth_state": "unknown",
        "reports": [],
        "selected": None,
        "comparison": None,
        "errors": [],
    }


def test_report_provider_receives_exact_context_scope_and_verifier(tmp_path: Path):
    rig = Rig(target="/api/v1/reports/projection")
    calls = []

    class Provider:
        def read(self, context, query, home, *, currentness_verifier):
            assert context.binding == rig.binding
            assert home == tmp_path
            assert currentness_verifier.check_before_owner_read(context).value == "allow"
            calls.append(dict(query))
            assert currentness_verifier.check_after_owner_read(context).value == "allow"
            return projection(query)

    app = create_app(
        tmp_path,
        control_plane_decision_authorizer=rig.authorizer,
        control_plane_invocation_factory=rig.factory,
        control_plane_report_provider=Provider(),
    )
    response = TestClient(app).get(
        PATH,
        headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert calls == [
        {
            "role": "project-manager",
            "scope": "estate",
            "window": "latest",
            "baseline": "none",
            "service": "all",
            "report_type": "all",
        }
    ]
    assert response.headers["etag"]


def test_report_detail_is_get_only_etagged_and_uses_provider(tmp_path: Path):
    rig = Rig(target="/api/v1/reports/rpt-example-report")

    class Provider:
        def read_snapshot(self, context, snapshot_id, home, *, currentness_verifier):
            assert context.binding == rig.binding
            assert snapshot_id == "rpt-example-report"
            assert home == tmp_path
            assert currentness_verifier.check_before_owner_read(context).value == "allow"
            assert currentness_verifier.check_after_owner_read(context).value == "allow"
            return snapshot()

    app = create_app(
        tmp_path,
        control_plane_decision_authorizer=rig.authorizer,
        control_plane_invocation_factory=rig.factory,
        control_plane_report_provider=Provider(),
    )
    headers = {"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN}
    response = TestClient(app).get("/api/v1/reports/rpt-example-report", headers=headers)
    assert response.status_code == 200
    assert response.json()["snapshot_id"] == "rpt-example-report"
    assert response.headers["etag"] == '"' + "a" * 64 + '"'
    assert (
        TestClient(app).post("/api/v1/reports/rpt-example-report", headers=headers).status_code
        == 405
    )


def test_report_scope_rejects_unknown_duplicate_and_protected_values(tmp_path: Path):
    class Provider:
        def read(self, *_args, **_kwargs):
            raise AssertionError("provider must not run")

    for suffix in ("&unknown=value", "&role=operator", "&tenant_id=protected"):
        rig = Rig(target="/api/v1/reports/projection")
        app = create_app(
            tmp_path,
            control_plane_decision_authorizer=rig.authorizer,
            control_plane_invocation_factory=rig.factory,
            control_plane_report_provider=Provider(),
        )
        response = TestClient(app).get(
            PATH + suffix,
            headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_REPORT_SCOPE"


def test_report_provider_requires_typed_authorization(tmp_path: Path):
    try:
        create_app(tmp_path, control_plane_report_provider=object())
    except ValueError as error:
        assert "typed control-plane authorization" in str(error)
    else:
        raise AssertionError("report provider must fail closed without typed authorization")
