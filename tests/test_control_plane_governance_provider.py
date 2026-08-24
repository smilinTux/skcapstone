from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient
from test_control_plane_decision_context import ORIGIN, Rig

from skdashboard.dashboard import create_app

PATH = "/api/v1/governance/projection?role=governance&scope=estate&window=latest&baseline=none&service=all"


def projection(query):
    return {
        "schema_version": "1.0.0",
        "projection_id": "governance-1",
        "projection_hash": "sha256:" + "a" * 64,
        "scope": query,
        "truth_state": "current",
        "metric_lineage": [],
        "source_lineage": [],
        "findings": [],
        "correction_history": [],
        "errors": [],
    }


def test_governance_provider_receives_exact_context_scope_and_verifier(tmp_path: Path) -> None:
    rig = Rig(target="/api/v1/governance/projection")
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
        control_plane_governance_provider=Provider(),
    )
    response = TestClient(app).get(
        PATH,
        headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["projection_id"] == "governance-1"
    assert calls == [
        {
            "role": "governance",
            "scope": "estate",
            "window": "latest",
            "baseline": "none",
            "service": "all",
        }
    ]
    assert response.headers["etag"]


def test_governance_scope_rejects_unknown_duplicate_and_protected_values(tmp_path: Path) -> None:
    class Provider:
        def read(self, *_args, **_kwargs):
            raise AssertionError("provider must not run")

    for suffix in ("&unknown=value", "&role=operator", "&tenant_id=protected"):
        rig = Rig(target="/api/v1/governance/projection")
        app = create_app(
            tmp_path,
            control_plane_decision_authorizer=rig.authorizer,
            control_plane_invocation_factory=rig.factory,
            control_plane_governance_provider=Provider(),
        )
        response = TestClient(app).get(
            PATH + suffix,
            headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
        )
        assert response.status_code == 400
        assert response.json()["code"] == "INVALID_GOVERNANCE_SCOPE"


def test_governance_provider_requires_typed_authorization(tmp_path: Path) -> None:
    class Provider:
        pass

    try:
        create_app(tmp_path, control_plane_governance_provider=Provider())
    except ValueError as error:
        assert "typed control-plane authorization" in str(error)
    else:
        raise AssertionError("governance provider must fail closed without typed authorization")
