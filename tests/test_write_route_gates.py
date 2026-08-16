"""Proves the three write routes that used to bypass the authz gate entirely
now go through it (card 9d37d53d).

BEFORE this card, ``POST /api/card/{id}/{action}``, ``POST /api/cmdb/seed`` and
``POST /api/models/advertise`` reached the coordination store, the CMDB, and
skgateway's advertise allowlist without ever consulting ``queue_authz``. Their
only control was the ``127.0.0.1`` bind, so any process able to open the
loopback port could mutate the board or the fleet's model catalog. The queue and
change.* routes were already gated; these three simply were not wired in.

Invariants proved here:
  1. With the gate configured (``SKAI_QUEUE_TOKEN`` set) and no/wrong
     capability, each of the three routes returns 403 AND its underlying
     side effect never runs.
  2. With the right capability, each route proceeds and does its work.
  3. With NEITHER ``SKAI_AUTHZ`` nor ``SKAI_QUEUE_TOKEN`` set, each route still
     works. This is the deployed loopback-open state on the live seat, and this
     card must not change it.
  4. Registry sweep: EVERY POST route registered on the app carries one of the
     known gate markers in its handler source. A future POST route added
     without a gate fails this test instead of shipping ungated.

What this file does NOT prove, deliberately: that ``X-SK-Actor`` is
authenticated. It is not. It is a self-asserted header, and closing that hole
belongs to the Unified Consent Plane epic (capauth ``x-sk-capability``), not to
this card. See SECURITY.md.
"""

from __future__ import annotations

import inspect
import json
import tempfile
from pathlib import Path

import pytest
from test_queue_gate_enforcement import FakeRequest, _call, _route_endpoint

from skdashboard.dashboard import create_app


class BodyRequest(FakeRequest):
    """FakeRequest plus the async ``.body()`` that ``api_models_advertise`` reads."""

    async def body(self):
        return json.dumps(self._json_body).encode("utf-8")


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app(home):
    return create_app(home)


@pytest.fixture
def gate_on(monkeypatch):
    """Configure the gate so the loopback-open carve-out no longer applies."""
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")


@pytest.fixture
def gate_off(monkeypatch):
    """The deployed live-seat state: neither authz variable set."""
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)


@pytest.fixture
def spy_apply_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "skdashboard.dashboard_kanban.apply_mutation",
        lambda *a, **k: (calls.append((a, k)), {"ok": True})[1],
    )
    return calls


@pytest.fixture
def spy_cmdb_seed(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "skdashboard.dashboard_cmdb.seed",
        lambda *a, **k: (calls.append((a, k)), {"ok": True, "seeded": 0})[1],
    )
    return calls


@pytest.fixture
def spy_urlopen(monkeypatch):
    """Spy on the outbound PUT to skgateway's /admin/models/advertise."""
    calls = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"ok": true}'

    def _fake_urlopen(req, timeout=None):
        calls.append(getattr(req, "full_url", req))
        return _Resp()

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    return calls


# --------------------------------------------------------------------------- #
# Invariant 1: gate configured + no/wrong capability -> 403, no side effect
# --------------------------------------------------------------------------- #
def test_card_mutate_denies_and_does_not_mutate_without_capability(
    app, gate_on, spy_apply_mutation
):
    handler = _route_endpoint(app, "/api/card/{card_id}/{action}")
    request = FakeRequest(
        headers={"x-sk-actor": "attacker"},  # no X-SK-Capability at all
        path_params={"card_id": "task-1", "action": "move"},
        json_body={"column": "done"},
    )
    response = _call(handler, request)

    assert response.status_code == 403
    assert "unauthorized" in json.loads(response.body)["error"]
    assert spy_apply_mutation == []


def test_card_mutate_denies_on_wrong_capability(app, gate_on, spy_apply_mutation):
    handler = _route_endpoint(app, "/api/card/{card_id}/{action}")
    request = FakeRequest(
        headers={"x-sk-capability": "wrong-token"},
        path_params={"card_id": "task-1", "action": "note"},
        json_body={"text": "hi"},
    )
    response = _call(handler, request)

    assert response.status_code == 403
    assert spy_apply_mutation == []


def test_cmdb_seed_denies_and_does_not_seed_without_capability(app, gate_on, spy_cmdb_seed):
    handler = _route_endpoint(app, "/api/cmdb/seed")
    response = _call(handler, FakeRequest(headers={}))

    assert response.status_code == 403
    assert spy_cmdb_seed == []


def test_models_advertise_denies_and_does_not_call_gateway_without_capability(
    app, gate_on, spy_urlopen
):
    handler = _route_endpoint(app, "/api/models/advertise")
    request = BodyRequest(headers={}, json_body={"enabled": ["evil-model"]})
    response = _call(handler, request)

    assert response.status_code == 403
    assert spy_urlopen == [], "a denied request must never reach skgateway's admin surface"


# --------------------------------------------------------------------------- #
# Invariant 2: right capability -> the route does its work
# --------------------------------------------------------------------------- #
def test_card_mutate_proceeds_with_valid_capability(app, gate_on, spy_apply_mutation):
    handler = _route_endpoint(app, "/api/card/{card_id}/{action}")
    request = FakeRequest(
        headers={"x-sk-capability": "sekrit", "x-sk-actor": "chef"},
        path_params={"card_id": "task-7", "action": "move"},
        json_body={"column": "done"},
    )
    response = _call(handler, request)

    assert response.status_code == 200
    assert len(spy_apply_mutation) == 1
    args, _kwargs = spy_apply_mutation[0]
    # (home, card_id, action, actor) - the actor still comes from X-SK-Actor.
    assert args[1:4] == ("task-7", "move", "chef")


def test_cmdb_seed_proceeds_with_valid_capability(app, gate_on, spy_cmdb_seed):
    handler = _route_endpoint(app, "/api/cmdb/seed")
    response = _call(handler, FakeRequest(headers={"x-sk-capability": "sekrit"}))

    assert response.status_code == 200
    assert len(spy_cmdb_seed) == 1


def test_models_advertise_proceeds_with_valid_capability(app, gate_on, spy_urlopen):
    handler = _route_endpoint(app, "/api/models/advertise")
    request = BodyRequest(
        headers={"x-sk-capability": "sekrit"}, json_body={"enabled": ["sk-default"]}
    )
    response = _call(handler, request)

    assert response.status_code == 200
    assert len(spy_urlopen) == 1
    assert spy_urlopen[0].endswith("/admin/models/advertise")


# --------------------------------------------------------------------------- #
# Invariant 3: the live seat (neither variable set) is unchanged
# --------------------------------------------------------------------------- #
def test_card_mutate_still_open_when_no_authz_var_is_set(app, gate_off, spy_apply_mutation):
    """The dashboard runs today with neither variable set. Gating these routes
    must not break that seat: loopback-open still allows, exactly as it does for
    the queue and change.* routes."""
    handler = _route_endpoint(app, "/api/card/{card_id}/{action}")
    request = FakeRequest(
        headers={}, path_params={"card_id": "task-1", "action": "note"}, json_body={"text": "hi"}
    )
    response = _call(handler, request)

    assert response.status_code == 200
    assert len(spy_apply_mutation) == 1


def test_cmdb_seed_still_open_when_no_authz_var_is_set(app, gate_off, spy_cmdb_seed):
    handler = _route_endpoint(app, "/api/cmdb/seed")
    response = _call(handler, FakeRequest(headers={}))

    assert response.status_code == 200
    assert len(spy_cmdb_seed) == 1


def test_models_advertise_still_open_when_no_authz_var_is_set(app, gate_off, spy_urlopen):
    handler = _route_endpoint(app, "/api/models/advertise")
    response = _call(handler, BodyRequest(headers={}, json_body={"enabled": []}))

    assert response.status_code == 200
    assert len(spy_urlopen) == 1


# --------------------------------------------------------------------------- #
# Invariant 4: no POST route escapes the gate
# --------------------------------------------------------------------------- #
#: Any one of these appearing in a POST handler's source means the route is
#: gated. ``_queue_run`` and ``_ai_capability_ok`` are themselves gate callers,
#: proved by tests/test_queue_gate_enforcement.py.
_GATE_MARKERS = (
    "_capability_gate",
    "_queue_gate",
    "_change_gate",
    "_queue_run",
    "_ai_capability_ok",
)


def test_every_post_route_carries_a_gate_marker(app):
    """Sweep the real route table: every registered POST endpoint must call a
    gate. This is the regression tripwire for the class of defect this card
    fixed, where a write route was simply never wired into ``queue_authz``.

    A new POST route that legitimately needs no gate must be added to an
    explicit exemption here with a reason, so the exemption is reviewed rather
    than silent. There are no exemptions today.
    """
    ungated = []
    checked = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "POST" not in methods:
            continue
        path = route.path
        checked.append(path)
        source = inspect.getsource(route.endpoint)
        if not any(marker in source for marker in _GATE_MARKERS):
            ungated.append(path)

    assert checked, "no POST routes found - the sweep is not actually looking at anything"
    assert ungated == [], (
        f"POST routes with no authorization gate: {ungated}. Every write route must "
        "call _capability_gate (or one of its named wrappers) before it mutates "
        "anything; see SECURITY.md 'Gated vs ungated routes'."
    )


def test_the_three_previously_ungated_routes_are_in_the_sweep(app):
    """Negative control for the sweep above: if the sweep silently stopped
    seeing these three paths it would pass while proving nothing."""
    post_paths = {
        r.path for r in app.routes if "POST" in (getattr(r, "methods", None) or set())
    }
    assert {
        "/api/card/{card_id}/{action}",
        "/api/cmdb/seed",
        "/api/models/advertise",
    } <= post_paths
