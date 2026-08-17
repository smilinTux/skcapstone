"""GET /api/auth/capability - the endpoint that lets a client obtain its
x-sk-capability + actor instead of hardcoding "operator" (Unified Consent
Plane P1.3, coord card a638b490).

Deliberately narrow: this route does not itself gate anything (it IS the
seam a client calls before it can pass a gate at all), so there is no
403/allow invariant to prove here, unlike test_write_route_gates.py. What
matters is that it echoes the exact values `_capability_gate`'s token check
and the PDP's `actor` argument consume, with the documented fail-closed
defaults when unconfigured.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from test_queue_gate_enforcement import FakeRequest, _call, _route_endpoint

from skdashboard.dashboard import create_app


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app(home):
    return create_app(home)


def _handout(app):
    handler = _route_endpoint(app, "/api/auth/capability")
    response = _call(handler, FakeRequest())
    return json.loads(response.body)


def test_unconfigured_seat_returns_fail_closed_defaults(app, monkeypatch):
    """No SKAI_QUEUE_TOKEN / SKAI_OPERATOR_ACTOR set: capability is null and
    actor is "unattributed", never the hardcoded "operator" this card retires
    -- "unattributed" is not an enrolled capauth subject, so a client that
    never got a real actor configured fails the PDP openly instead of
    silently asserting an identity nobody granted.
    """
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)
    monkeypatch.delenv("SKAI_OPERATOR_ACTOR", raising=False)

    body = _handout(app)

    assert body == {"capability": None, "actor": "unattributed"}


def test_configured_seat_echoes_both_values(app, monkeypatch):
    """The exact values _capability_gate's token check and the PDP actor
    argument consume, so a client that presents them back never fails its
    own gate.
    """
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    monkeypatch.setenv("SKAI_OPERATOR_ACTOR", "capauth:chef@skworld.io")

    body = _handout(app)

    assert body == {"capability": "sekrit", "actor": "capauth:chef@skworld.io"}


def test_blank_operator_actor_degrades_to_unattributed(app, monkeypatch):
    """An env var set to whitespace/empty must not leak through as a truthy
    but meaningless actor string.
    """
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)
    monkeypatch.setenv("SKAI_OPERATOR_ACTOR", "   ")

    body = _handout(app)

    assert body["actor"] == "unattributed"


def test_handed_out_capability_then_satisfies_the_token_gate(app, monkeypatch):
    """End-to-end within this process: what the handout returns is exactly
    what a client needs to attach to pass _capability_gate's token check
    (mirrors what api.js's authHeaders() now does before every mutating
    call).
    """
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)  # -> default "token" mode
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")

    cap = _handout(app)

    handler = _route_endpoint(app, "/api/cmdb/seed")
    request = FakeRequest(headers={"x-sk-capability": cap["capability"]})
    response = _call(handler, request)

    assert response.status_code != 403, response.body
