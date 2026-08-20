"""Verified human CAB vote route and identity binding tests."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from skcoord.itil import ITILManager

from skdashboard import consent
from skdashboard.dashboard import create_app


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)


class FakeRequest:
    def __init__(self, *, headers=None, path_params=None, json_body=None):
        self.headers = _FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
        self.path_params = path_params or {}
        self.query_params = {}
        self._json_body = {} if json_body is None else json_body

    async def json(self):
        return self._json_body


@dataclass
class _OperatorSession:
    jti: str = "cab-session-1"
    device_fp: str = "0a1b2c3d4e5f6789"
    exp: int = 9999999999


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)
    return create_app(tmp_path)


def _handler(app):
    return next(route.endpoint for route in app.routes if route.path == "/api/change/{id}/cab-vote")


def _call(handler, request):
    return asyncio.run(handler(request))


def _change(home):
    return ITILManager(home).propose_change(
        title="CAB route test", change_type="normal", managed_by="lumina"
    )


def _verify_good(monkeypatch):
    monkeypatch.setattr(
        "capauth.pairing.verify_operator_session", lambda token: _OperatorSession()
    )


def _request(change_id, decision="approved", *, token="good-token", capability=None):
    headers = {"x-operator-token": token, "x-sk-actor": "forged-actor"}
    if capability is not None:
        headers["x-sk-capability"] = capability
    return FakeRequest(
        headers=headers,
        path_params={"id": change_id},
        json_body={"decision": decision, "conditions": "smoke checks must pass"},
    )


@pytest.mark.parametrize("headers", [{}, {"x-operator-token": "expired"}])
def test_cab_vote_requires_verified_operator_session(app, tmp_path, monkeypatch, headers):
    monkeypatch.setattr(
        "capauth.pairing.verify_operator_session",
        lambda token: (_ for _ in ()).throw(ValueError("bad token")),
    )
    chg = _change(tmp_path)
    response = _call(
        _handler(app),
        FakeRequest(
            headers=headers,
            path_params={"id": chg.id},
            json_body={"decision": "approved"},
        ),
    )

    assert response.status_code == 401
    assert ITILManager(tmp_path).get_cab_votes(chg.id) == []


def test_verified_approval_records_human_vote_and_operator_audit(
    app, tmp_path, monkeypatch
):
    _verify_good(monkeypatch)
    chg = _change(tmp_path)

    response = _call(_handler(app), _request(chg.id))

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["status"] == "approved"
    assert body["operator"] == "device:0a1b2c3d4e5f6789"
    assert body["verified"] is True
    mgr = ITILManager(tmp_path)
    votes = mgr.get_cab_votes(chg.id)
    assert len(votes) == 1
    assert votes[0].agent == "human"
    assert "verified operator device:0a1b2c3d4e5f6789" in votes[0].conditions
    events = consent.consent_history_for_change(mgr, chg.id)
    assert events[-1]["actor"]["id"] == "device:0a1b2c3d4e5f6789"
    assert events[-1]["actor"]["session"] == "opsess:cab-session-1"
    assert events[-1]["capability"] == "change.cab_vote"


@pytest.mark.parametrize(
    "decision,expected_status", [("rejected", "rejected"), ("abstain", "proposed")]
)
def test_verified_reject_and_abstain(
    app, tmp_path, monkeypatch, decision, expected_status
):
    _verify_good(monkeypatch)
    chg = _change(tmp_path)

    response = _call(_handler(app), _request(chg.id, decision))

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == expected_status


def test_wrong_capability_denies_without_vote(app, tmp_path, monkeypatch):
    _verify_good(monkeypatch)
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "required-token")
    chg = _change(tmp_path)

    response = _call(_handler(app), _request(chg.id, capability="wrong-token"))

    assert response.status_code == 403
    assert ITILManager(tmp_path).get_cab_votes(chg.id) == []


def test_vote_refuses_non_review_state(app, tmp_path, monkeypatch):
    _verify_good(monkeypatch)
    chg = _change(tmp_path)
    ITILManager(tmp_path).submit_cab_vote(chg.id, "human", "approved")

    response = _call(_handler(app), _request(chg.id))

    assert response.status_code == 409
    assert json.loads(response.body)["status"] == "approved"


def test_vote_unknown_change_is_404(app, monkeypatch):
    _verify_good(monkeypatch)

    response = _call(_handler(app), _request("chg-doesnotexist"))

    assert response.status_code == 404


def test_human_drafter_cannot_self_approve(app, tmp_path, monkeypatch):
    _verify_good(monkeypatch)
    chg = _change(tmp_path)
    mgr = ITILManager(tmp_path)
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "human",
        "pr_link",
        url="https://github.com/smilinTux/skdashboard/pull/999",
        branch="fix/cab-test",
        run_id="run-test",
        head_sha="deadbeef",
    )

    response = _call(_handler(app), _request(chg.id))

    assert response.status_code == 200
    assert json.loads(response.body)["status"] == "proposed"
