"""Change-mgmt P2.3 (68cc2079): the validate/schedule/arm PEP routes, and
P2.4's kanban chip payload (a2ba/dashboard_kanban.py).

Drives the real route handlers (extracted from
:func:`skdashboard.dashboard.create_app`) with a fake Starlette request,
mirroring the pattern in test_queue_gate_enforcement.py. Seeds ITIL change
records directly through :class:`skcoord.itil.ITILManager` into a tmp agent
home so neither test touches the live ``~/.skcapstone/coordination/itil/``.

Covered:
  1. All three routes fail closed (403) on a bad/absent capability token, and
     are loopback-open only when neither SKAI_AUTHZ nor SKAI_QUEUE_TOKEN is
     configured (mirrors the existing queue-ai gate's documented dev bypass).
  2. /validate refuses (409, no event appended) when the change has no
     prepared_pr; on prepared_pr + passing gh checks, appends the validation
     event and the change auto-advances proposed -> reviewing via the fold
     (no redundant status event).
  3. /schedule appends a schedule event (asap and explicit-window forms),
     refuses (409) when the change is not approved, and supports unschedule
     via {"unschedule": true}.
  4. /schedule rejects any deploy_mode other than "confirm".
  5. /arm writes cab-decisions/<chg>-<agent>.arm.json and 404s on an unknown
     change id.
  6. The kanban chip payload (CAB tally / validation verdict / window) on a
     change card's api_kanban brief.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from skcoord.itil import ITILManager

from skdashboard.dashboard import create_app


# --------------------------------------------------------------------------- #
# Fake Starlette Request (mirrors test_queue_gate_enforcement.py)
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
    """Default every test to the loopback-open dev gate; individual auth
    tests override this explicitly."""
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)


@pytest.fixture(autouse=True)
def _no_gh_calls(monkeypatch):
    """No test should ever shell out to a real `gh` binary; validate tests
    that need specific behavior override these individually."""

    def _no_checks(pr_url):
        return {"started": False, "passed": False, "checks": [], "error": "not mocked"}

    monkeypatch.setattr("skdashboard.dashboard._gh_pr_checks", _no_checks)
    monkeypatch.setattr("skdashboard.dashboard._gh_trigger_checks", lambda *a, **k: False)
    monkeypatch.setattr("skdashboard.dashboard._gh_pr_head_sha", lambda *a, **k: None)


def _seed_prepared_change(home, *, status="proposed", head_sha="deadbeef"):
    """A change with a prepared_pr, at the given approval status."""
    mgr = ITILManager(home)
    change_type = "standard" if status == "approved" else "normal"
    chg = mgr.propose_change(title="seeded change", change_type=change_type, managed_by="lumina")
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://github.com/smilinTux/skdashboard/pull/7",
        branch="chg/seeded",
        run_id="run-1",
        head_sha=head_sha,
    )
    return mgr.list_changes()[0].id


def _seed_approved_change(home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="approved change", change_type="standard", managed_by="lumina")
    return chg.id


# --------------------------------------------------------------------------- #
# 1. Fail-closed auth, all three routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path,capability",
    [
        ("/api/change/{id}/validate", "change.validate"),
        ("/api/change/{id}/schedule", "change.schedule"),
        ("/api/change/{id}/arm", "change.deploy"),
    ],
)
def test_change_routes_deny_on_wrong_capability_token(app, monkeypatch, home, path, capability):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, path)
    request = FakeRequest(
        headers={"x-sk-capability": "wrong"},
        path_params={"id": change_id},
        json_body={},
    )
    response = _call(handler, request)

    assert response.status_code == 403
    body = json.loads(response.body)
    assert "unauthorized" in body["error"]


@pytest.mark.parametrize(
    "path",
    ["/api/change/{id}/validate", "/api/change/{id}/schedule", "/api/change/{id}/arm"],
)
def test_change_routes_deny_on_absent_capability_token(app, monkeypatch, home, path):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, path)
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 403


def test_change_routes_loopback_open_when_neither_authz_var_set(app, home):
    """No SKAI_AUTHZ/SKAI_QUEUE_TOKEN configured -> loopback-open, same
    documented dev bypass as the existing queue-ai gate."""
    change_id = _seed_approved_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 200
    assert json.loads(response.body)["armed"] is True


def test_change_arm_denies_and_writes_nothing_on_wrong_capability(
    app, monkeypatch, home, tmp_path
):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(
        headers={"x-sk-capability": "wrong"}, path_params={"id": change_id}, json_body={}
    )
    response = _call(handler, request)

    assert response.status_code == 403
    mgr = ITILManager(home)
    assert list(mgr.cab_dir.glob("*.arm.json")) == [] if mgr.cab_dir.exists() else True


def test_change_actor_ignores_body_actor_field_uses_header_only(app, home):
    """The acting subject must come from X-SK-Actor, never a client-supplied
    body field - proves the PEP does not trust a client-claimed identity."""
    change_id = _seed_approved_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"},
        path_params={"id": change_id},
        json_body={"actor": "someone-else", "requester": "also-someone-else"},
    )
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["agent"] == "chef"


# --------------------------------------------------------------------------- #
# 2. /validate
# --------------------------------------------------------------------------- #
def test_validate_refuses_without_prepared_pr(app, home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="no pr yet", change_type="normal", managed_by="lumina")

    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(headers={}, path_params={"id": chg.id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert "prepared_pr" in body["error"]
    # No validation event was appended.
    assert mgr.list_changes()[0].validation is None


def test_validate_unknown_change_404s(app, home):
    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(headers={}, path_params={"id": "chg-doesnotexist"}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 404


def test_validate_pass_appends_validation_event_and_advances_to_reviewing(app, monkeypatch, home):
    change_id = _seed_prepared_change(home, status="proposed", head_sha="deadbeef")

    def _passing_checks(pr_url):
        assert pr_url == "https://github.com/smilinTux/skdashboard/pull/7"
        return {
            "started": True,
            "passed": True,
            "checks": [{"name": "pytest", "bucket": "pass"}],
            "error": None,
        }

    monkeypatch.setattr("skdashboard.dashboard._gh_pr_checks", _passing_checks)
    monkeypatch.setattr("skdashboard.dashboard._gh_pr_head_sha", lambda pr_url: "deadbeef")

    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"}, path_params={"id": change_id}, json_body={}
    )
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["validated"] is True
    assert body["status"] == "reviewing"
    assert body["validation"]["passed"] is True
    assert body["validation"]["head_sha"] == "deadbeef"

    mgr = ITILManager(home)
    chg = mgr.list_changes()[0]
    assert chg.status.value == "reviewing"
    # Exactly one validation event was appended - no redundant status event
    # (the fold's own auto-transition handles proposed -> reviewing).
    events = mgr._read_events(mgr.changes_dir, change_id)
    validation_events = [e for e in events if e.get("kind") == "validation"]
    status_events = [e for e in events if e.get("kind") == "status"]
    assert len(validation_events) == 1
    assert status_events == []


def test_validate_fail_leaves_status_unchanged(app, monkeypatch, home):
    change_id = _seed_prepared_change(home, status="proposed")

    def _failing_checks(pr_url):
        return {
            "started": True,
            "passed": False,
            "checks": [{"name": "pytest", "bucket": "fail"}],
            "error": None,
        }

    monkeypatch.setattr("skdashboard.dashboard._gh_pr_checks", _failing_checks)

    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["validation"]["passed"] is False
    assert body["status"] == "proposed"


def test_validate_triggers_workflow_when_checks_have_not_started(app, monkeypatch, home):
    change_id = _seed_prepared_change(home, status="proposed")
    calls = {"triggered": False, "poll_count": 0}

    def _checks(pr_url):
        calls["poll_count"] += 1
        if calls["poll_count"] == 1:
            return {"started": False, "passed": False, "checks": [], "error": None}
        return {"started": True, "passed": True, "checks": [{"bucket": "pass"}], "error": None}

    def _trigger(pr_url, branch):
        calls["triggered"] = True
        return True

    monkeypatch.setattr("skdashboard.dashboard._gh_pr_checks", _checks)
    monkeypatch.setattr("skdashboard.dashboard._gh_trigger_checks", _trigger)

    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 200
    assert calls["triggered"] is True
    assert calls["poll_count"] == 2


# --------------------------------------------------------------------------- #
# 3. /schedule
# --------------------------------------------------------------------------- #
def test_schedule_asap_appends_schedule_event(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={"asap": True})
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["scheduled"] is True
    assert body["status"] == "scheduled"
    assert body["scheduled_window"]["asap"] is True
    assert body["scheduled_window"]["deploy_mode"] == "confirm"


def test_schedule_explicit_window_appends_schedule_event(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(
        headers={},
        path_params={"id": change_id},
        json_body={
            "window_start": "2026-08-20T02:00:00+00:00",
            "window_end": "2026-08-20T06:00:00+00:00",
        },
    )
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["scheduled"] is True
    assert body["scheduled_window"]["window_start"] == "2026-08-20T02:00:00+00:00"
    assert body["scheduled_window"]["asap"] is False


def test_schedule_without_window_or_asap_400s(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 400


def test_schedule_refuses_when_not_approved(app, home):
    """A proposed (not yet approved) change: fold refuses the schedule
    transition - the route must surface that as a failed schedule, not
    silently succeed."""
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="not approved yet", change_type="normal", managed_by="lumina")

    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(headers={}, path_params={"id": chg.id}, json_body={"asap": True})
    response = _call(handler, request)

    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["scheduled"] is False
    assert body["status"] == "proposed"


def test_schedule_deploy_mode_locked_to_confirm(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(
        headers={},
        path_params={"id": change_id},
        json_body={"asap": True, "deploy_mode": "auto"},
    )
    response = _call(handler, request)

    assert response.status_code == 400
    body = json.loads(response.body)
    assert "confirm" in body["error"]

    # No schedule event was appended - the change is still merely approved.
    mgr = ITILManager(home)
    assert mgr.list_changes()[0].status.value == "approved"


def test_schedule_unschedule_returns_to_approved(app, home):
    change_id = _seed_approved_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/schedule")

    schedule_req = FakeRequest(headers={}, path_params={"id": change_id}, json_body={"asap": True})
    _call(handler, schedule_req)

    unschedule_req = FakeRequest(
        headers={}, path_params={"id": change_id}, json_body={"unschedule": True}
    )
    response = _call(handler, unschedule_req)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["unscheduled"] is True
    assert body["status"] == "approved"

    mgr = ITILManager(home)
    assert mgr.list_changes()[0].scheduled_window is None


def test_schedule_unknown_change_404s(app, home):
    handler = _route_endpoint(app, "/api/change/{id}/schedule")
    request = FakeRequest(
        headers={}, path_params={"id": "chg-doesnotexist"}, json_body={"asap": True}
    )
    response = _call(handler, request)

    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# 4. /arm
# --------------------------------------------------------------------------- #
def test_arm_writes_the_arm_file(app, home):
    change_id = _seed_approved_change(home)

    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(
        headers={"x-sk-actor": "chef"},
        path_params={"id": change_id},
        json_body={"note": "go for it"},
    )
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["armed"] is True
    assert body["agent"] == "chef"

    mgr = ITILManager(home)
    path = mgr.cab_dir / f"{change_id}-chef.arm.json"
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["change_id"] == change_id
    assert data["agent"] == "chef"
    assert data["armed"] is True
    assert data["note"] == "go for it"


def test_arm_unknown_change_404s(app, home):
    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(headers={}, path_params={"id": "chg-doesnotexist"}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 404


def test_arm_defaults_actor_to_operator_when_no_header(app, home):
    change_id = _seed_approved_change(home)
    handler = _route_endpoint(app, "/api/change/{id}/arm")
    request = FakeRequest(headers={}, path_params={"id": change_id}, json_body={})
    response = _call(handler, request)

    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["agent"] == "operator"


# --------------------------------------------------------------------------- #
# 5. Kanban chip payload (CM P2.4)
# --------------------------------------------------------------------------- #
def _get_kanban(app):
    handler = _route_endpoint(app, "/api/kanban")
    response = _call(handler, FakeRequest())
    return json.loads(response.body)


def _change_brief(kanban: dict, change_id: str) -> dict:
    for lane in kanban["lanes"]:
        if lane["key"] != "change":
            continue
        for col_cards in lane["columns"].values():
            for card in col_cards:
                if card["id"] == change_id:
                    return card
    raise AssertionError(f"{change_id} not found on the change lane")


@pytest.fixture(autouse=True)
def _legacy_card_projection(monkeypatch):
    """KanbanBoard.cards() defaults to the event-sourced CardStore
    (card_store_read_enabled() is default-ON), which has no independent ITIL
    projection of its own - these tests seed data through ITILManager
    directly, so they need the legacy coord+ITIL projection instead."""
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")


def test_kanban_chip_payload_shape_on_a_bare_change(app, home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="chip test bare", change_type="normal", managed_by="lumina")

    card = _change_brief(_get_kanban(app), chg.id)

    assert card["itil_status"] == "proposed"
    assert card["prepared_pr"] is None
    assert card["validation"] is None
    assert card["scheduled_window"] is None
    assert card["chips"]["cab"] == {
        "approved": 0,
        "rejected": 0,
        "abstain": 0,
        "human_decision": None,
    }
    assert card["chips"]["validation"] is None
    assert card["chips"]["window"] == {"label": "none", "asap": False}


def test_kanban_chip_payload_cab_tally_and_human_decision(app, home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="chip test cab", change_type="normal", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="opus", decision="approved")
    mgr.submit_cab_vote(chg.id, agent="jarvis", decision="abstain")
    mgr.submit_cab_vote(chg.id, agent="human-caller", decision="approved", subject="human")

    card = _change_brief(_get_kanban(app), chg.id)

    assert card["chips"]["cab"]["approved"] == 2
    assert card["chips"]["cab"]["abstain"] == 1
    assert card["chips"]["cab"]["rejected"] == 0
    assert card["chips"]["cab"]["human_decision"] == "approved"


# The validation/window chip cases below test dashboard_kanban's own chip
# functions directly against a hand-built ``Card`` (``skcoord.card.Card``,
# ``Kind``, ``Column`` predate P2.4 and are stable), rather than going
# through ``get_kanban`` -> ``KanbanBoard`` -> ``card_from_change``. That
# keeps them decoupled from exactly which skcoord release/checkout supplies
# ``card_from_change``'s P2.4 meta passthrough (this worktree's sibling
# skcoord change, card 721fded0, landed separately) - they exercise
# dashboard_kanban.py's OWN logic, which is what this card actually owns.
# The two tests above (bare change, CAB tally) stay full end-to-end via the
# real route/app because neither depends on card_from_change's new fields:
# a bare proposed change's validation/scheduled_window are None either way,
# and the CAB tally chip reads votes straight from ITILManager, unaffected
# by which card.py is installed.


def _make_change_card(**meta):
    from skcoord.card import Card, Column, Kind

    return Card(
        id="chg-unit-test",
        kind=Kind.CHANGE,
        title="unit test change",
        status=Column.READY,
        swimlane="change",
        meta=meta,
    )


def test_change_chips_validation_verdict_and_staleness(home):
    from skdashboard.dashboard_kanban import _change_chips

    card = _make_change_card(
        prepared_pr={"url": "https://x/pull/9", "head_sha": "newsha"},
        validation={
            "passed": True,
            "head_sha": "oldsha",
            "checks": [{"name": "a", "bucket": "pass"}, {"name": "b", "bucket": "pass"}],
        },
        scheduled_window=None,
        window_missed=False,
    )

    chips = _change_chips(card, home)

    assert chips["validation"]["passed"] is True
    assert chips["validation"]["check_count"] == 2
    # verdict head_sha ("oldsha") != current prepared_pr head_sha ("newsha")
    assert chips["validation"]["stale"] is True


def test_change_chips_validation_not_stale_when_head_sha_matches(home):
    from skdashboard.dashboard_kanban import _change_chips

    card = _make_change_card(
        prepared_pr={"url": "https://x/pull/9", "head_sha": "samesha"},
        validation={"passed": False, "head_sha": "samesha", "checks": [{"bucket": "fail"}]},
        scheduled_window=None,
        window_missed=False,
    )

    chips = _change_chips(card, home)

    assert chips["validation"]["passed"] is False
    assert chips["validation"]["stale"] is False


def test_change_chips_window_asap(home):
    from skdashboard.dashboard_kanban import _change_chips

    card = _make_change_card(
        prepared_pr=None,
        validation=None,
        scheduled_window={
            "window_start": "2026-08-20T02:00:00+00:00",
            "window_end": "2026-08-20T06:00:00+00:00",
            "asap": True,
            "deploy_mode": "confirm",
        },
        window_missed=False,
    )

    chips = _change_chips(card, home)

    assert chips["window"] == {"label": "ASAP", "asap": True}


def test_change_chips_window_formatted_label(home):
    from datetime import datetime

    from skdashboard.dashboard_kanban import _change_chips

    start = "2026-08-21T02:00:00+00:00"
    card = _make_change_card(
        prepared_pr=None,
        validation=None,
        scheduled_window={
            "window_start": start,
            "window_end": "2026-08-21T06:00:00+00:00",
            "asap": False,
            "deploy_mode": "confirm",
        },
        window_missed=False,
    )

    chips = _change_chips(card, home)

    expected = datetime.fromisoformat(start).strftime("%a %H:%MZ")
    assert chips["window"] == {"label": expected, "asap": False}


def test_change_chips_window_missed(home):
    from skdashboard.dashboard_kanban import _change_chips

    card = _make_change_card(
        prepared_pr=None, validation=None, scheduled_window=None, window_missed=True
    )

    chips = _change_chips(card, home)

    assert chips["window"] == {"label": "MISSED", "asap": False}


def test_change_chips_window_none_when_never_scheduled(home):
    from skdashboard.dashboard_kanban import _change_chips

    card = _make_change_card(
        prepared_pr=None, validation=None, scheduled_window=None, window_missed=False
    )

    chips = _change_chips(card, home)

    assert chips["window"] == {"label": "none", "asap": False}
