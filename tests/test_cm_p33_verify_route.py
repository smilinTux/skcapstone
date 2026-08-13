"""Change-mgmt P3.3: the PIR (post-implementation review) lifecycle.

Covers the ``/api/change/{id}/verify`` and ``/api/change/{id}/pir-draft``
routes plus the kanban PIR chip, per
docs/specs/2026-08-13-change-management-cab-ai-arch.md section 3
("deployed -> verified: post-implementation review (smoke checks + PIR
note)"). Mirrors test_cm_p2_change_routes.py's fake-Starlette-request
pattern exactly.

Covered:
  1. /verify fails closed (403) on a bad/absent capability token, same
     staged token/pdp/both gate as validate/schedule/arm, loopback-open only
     when neither SKAI_AUTHZ nor SKAI_QUEUE_TOKEN is configured.
  2. /verify refuses (409) when the change is not currently `deployed`.
  3. /verify refuses (400) when the note is empty/whitespace-only, before
     ever appending an event.
  4. /verify appends the `status -> verified` event with the note and
     advances the folded status; the actor comes from X-SK-Actor, never a
     client-claimed body field.
  5. /pir-draft assembles a deterministic draft from the folded record
     (prepared_pr, validation verdict, the deployed timeline entry,
     rollback plan).
  6. The kanban PIR chip on a verified change's card face.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from skcoord.itil import ITILManager

from skdashboard.dashboard import _pir_draft, create_app


# --------------------------------------------------------------------------- #
# Fake Starlette Request (mirrors test_cm_p2_change_routes.py)
# --------------------------------------------------------------------------- #
class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)


class FakeRequest:
    def __init__(self, *, headers=None, path_params=None, json_body=None, query_params=None):
        self.headers = _FakeHeaders({k.lower(): v for k, v in (headers or {}).items()})
        self.path_params = path_params or {}
        self.query_params = query_params or {}
        self._json_body = {} if json_body is None else json_body

    async def json(self):
        return self._json_body


def _route_endpoint(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for path {path!r}")


def _call(handler, request):
    import asyncio

    return asyncio.run(handler(request))


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app(home):
    return create_app(home)


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch):
    """Default every test to the loopback-open dev gate; auth tests override
    this explicitly."""
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)


def _seed_deployed_change(home, *, deploy_note="deploy step reported success"):
    """A normal change walked all the way to `deployed`, human-approved."""
    mgr = ITILManager(home)
    chg = mgr.propose_change(
        title="deployed change", change_type="normal", managed_by="lumina", rollback_plan="revert"
    )
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://github.com/smilinTux/skdashboard/pull/11",
        branch="chg/deploy-me",
        run_id="run-1",
        head_sha="deadbeef",
    )
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "validation",
        passed=True,
        head_sha="deadbeef",
        url="https://ci/run/1",
        summary="9/9 checks passed",
    )
    mgr._append_event(mgr.changes_dir, chg.id, "lumina", "status", to="implementing", note="")
    mgr._append_event(mgr.changes_dir, chg.id, "lumina", "status", to="deployed", note=deploy_note)
    return mgr.list_changes()[0].id


def _seed_approved_change(home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="approved change", change_type="standard", managed_by="lumina")
    return chg.id


# --------------------------------------------------------------------------- #
# 1. Fail-closed auth
# --------------------------------------------------------------------------- #
def test_verify_denies_on_wrong_capability_token(app, monkeypatch, home):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={"x-sk-capability": "wrong"},
        path_params={"id": change_id},
        json_body={"note": "looks good"},
    )
    response = _call(handler, request)

    assert response.status_code == 403
    body = json.loads(response.body)
    assert "unauthorized" in body["error"]
    # No event was appended on the denied attempt.
    mgr = ITILManager(home)
    assert mgr.list_changes()[0].status.value == "deployed"


def test_verify_denies_on_absent_capability_token(app, monkeypatch, home):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={}, path_params={"id": change_id}, json_body={"note": "looks good"}
    )
    response = _call(handler, request)

    assert response.status_code == 403


def test_verify_loopback_open_when_neither_authz_var_set(app, home):
    change_id = _seed_deployed_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={}, path_params={"id": change_id}, json_body={"note": "smoke checks green"}
    )
    response = _call(handler, request)

    assert response.status_code == 200
    assert json.loads(response.body)["verified"] is True


def test_pir_draft_denies_on_wrong_capability_token(app, monkeypatch, home):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/pir-draft")
    request = FakeRequest(headers={"x-sk-capability": "wrong"}, path_params={"id": change_id})
    response = _call(handler, request)

    assert response.status_code == 403


# --------------------------------------------------------------------------- #
# 2. Refuse when not deployed
# --------------------------------------------------------------------------- #
def test_verify_refuses_when_not_deployed(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={}, path_params={"id": change_id}, json_body={"note": "premature verify"}
    )
    response = _call(handler, request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert "not deployed" in body["error"]
    assert body["status"] == "approved"
    mgr = ITILManager(home)
    events = mgr._read_events(mgr.changes_dir, change_id)
    assert [e for e in events if e.get("kind") == "status" and e.get("to") == "verified"] == []


def test_verify_unknown_change_404s(app, home):
    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={}, path_params={"id": "chg-doesnotexist"}, json_body={"note": "n/a"}
    )
    response = _call(handler, request)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 3. Refuse when the note is missing/blank
# --------------------------------------------------------------------------- #
def test_verify_refuses_without_a_note(app, home):
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert "PIR note" in body["error"]
    mgr = ITILManager(home)
    assert mgr.list_changes()[0].status.value == "deployed"
    events = mgr._read_events(mgr.changes_dir, change_id)
    assert [e for e in events if e.get("kind") == "status" and e.get("to") == "verified"] == []


def test_verify_refuses_on_whitespace_only_note(app, home):
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={"note": "   "})
    response = _call(handler, request)

    assert response.status_code == 400
    mgr = ITILManager(home)
    assert mgr.list_changes()[0].status.value == "deployed"


# --------------------------------------------------------------------------- #
# 4. Verify appends the note and advances status
# --------------------------------------------------------------------------- #
def test_verify_appends_note_and_advances_to_verified(app, home):
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"},
        path_params={"id": change_id},
        json_body={"note": "smoke checks green, latency nominal"},
    )
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["verified"] is True
    assert body["status"] == "verified"
    assert body["pir_note"] == "smoke checks green, latency nominal"

    mgr = ITILManager(home)
    chg = mgr.list_changes()[0]
    assert chg.status.value == "verified"
    verified_rows = [row for row in chg.timeline if row["action"] == "status:deployed->verified"]
    assert len(verified_rows) == 1
    assert verified_rows[0]["agent"] == "chef"
    assert verified_rows[0]["note"] == "smoke checks green, latency nominal"
    assert not verified_rows[0].get("conflicted")


def test_verify_actor_comes_from_header_not_body(app, home):
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"},
        path_params={"id": change_id},
        json_body={"note": "verified", "actor": "someone-else", "requester": "also-not-chef"},
    )
    response = _call(handler, request)

    assert response.status_code == 200
    mgr = ITILManager(home)
    chg = mgr.list_changes()[0]
    verified_rows = [row for row in chg.timeline if row["action"] == "status:deployed->verified"]
    assert verified_rows[0]["agent"] == "chef"


def test_verify_defaults_actor_to_operator_when_no_header(app, home):
    change_id = _seed_deployed_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={"note": "ok"})
    response = _call(handler, request)

    assert response.status_code == 200
    mgr = ITILManager(home)
    chg = mgr.list_changes()[0]
    verified_rows = [row for row in chg.timeline if row["action"] == "status:deployed->verified"]
    assert verified_rows[0]["agent"] == "operator"


# --------------------------------------------------------------------------- #
# 5. /pir-draft
# --------------------------------------------------------------------------- #
def test_pir_draft_route_assembles_from_the_record(app, home):
    change_id = _seed_deployed_change(home, deploy_note="deploy went clean")

    handler = _route_endpoint(app, "/api/change/{id}/pir-draft")
    request = FakeRequest(headers={}, path_params={"id": change_id})
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["id"] == change_id
    assert body["status"] == "deployed"
    draft = body["draft"]
    assert "deployed change" in draft
    assert "github.com/smilinTux/skdashboard/pull/11" in draft
    assert "PASSED" in draft
    assert "9/9 checks passed" in draft
    assert "deploy went clean" in draft
    assert "revert" in draft


def test_pir_draft_unknown_change_404s(app, home):
    handler = _route_endpoint(app, "/api/change/{id}/pir-draft")
    request = FakeRequest(headers={}, path_params={"id": "chg-doesnotexist"})
    response = _call(handler, request)

    assert response.status_code == 404


def test_pir_draft_function_directly_on_a_bare_change(home):
    """Unit-level: _pir_draft on a change with no prepared_pr/validation
    still returns a sane (if sparse) draft - never raises."""
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="bare change", change_type="normal", managed_by="lumina")
    folded = mgr.list_changes()[0]
    assert folded.id == chg.id

    draft = _pir_draft(folded)
    assert "bare change" in draft
    assert "none recorded" in draft  # no rollback_plan on file


# --------------------------------------------------------------------------- #
# 6. Kanban PIR chip
# --------------------------------------------------------------------------- #
def test_pir_chip_none_before_verification(home):
    from skdashboard.dashboard_kanban import _pir_chip

    change_id = _seed_deployed_change(home)
    assert _pir_chip(change_id, home) is None


def test_pir_chip_populated_after_verification(app, home):
    change_id = _seed_deployed_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/verify")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"},
        path_params={"id": change_id},
        json_body={"note": "smoke checks green"},
    )
    _call(handler, request)

    from skdashboard.dashboard_kanban import _pir_chip

    chip = _pir_chip(change_id, home)
    assert chip is not None
    assert chip["note"] == "smoke checks green"
    assert chip["agent"] == "chef"


def test_pir_chip_none_without_home():
    from skdashboard.dashboard_kanban import _pir_chip

    assert _pir_chip("chg-whatever", None) is None
