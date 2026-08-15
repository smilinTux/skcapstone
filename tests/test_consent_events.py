"""Tests for :mod:`skdashboard.consent` (Unified Consent Plane Phase 2, coord
card 90d23f56).

Covered:
  1. ``resolve_consent_actor`` prefers a verified capauth operator session
     over the self-asserted X-SK-Actor header, degrades to a canonicalized
     header value, and finally to the literal "unattributed" - never a
     synthesized placeholder, never raises.
  2. ``build_consent_event`` produces the PROVENANCE_AND_MUTATION_STANDARD.md
     section 1 envelope shape (``spe``, ``actor``, ``ts``, ``action``,
     ``target``, ``prior``, ``capability``, ``decision``, ``sig``).
  3. ``record_card_consent``/``consent_history_for_card`` and
     ``record_change_consent``/``consent_history_for_change`` round-trip
     through the real CardStore / ITILManager append-only stores - no new
     store.
  4. The real dashboard PEPs (queue-ai, validate, schedule, arm) each persist
     a ``consent.granted`` event on an allowed request.
  5. Fold determinism: two simulated writers (the CardStore's per-writer-file
     shape, the exact thing that makes Syncthing merges conflict-free)
     append consent.granted events in opposite arrival order; the folded
     read converges on the identical, chronologically-ordered result either
     way.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from skdashboard import consent


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


@pytest.fixture(autouse=True)
def _open_gate(monkeypatch):
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.delenv("SKAI_QUEUE_TOKEN", raising=False)


# --------------------------------------------------------------------------- #
# 1. resolve_consent_actor
# --------------------------------------------------------------------------- #
@dataclass
class _FakeOperatorSession:
    jti: str
    device_fp: str
    exp: int


def test_resolve_consent_actor_prefers_verified_operator_session(monkeypatch):
    def fake_verify(token, *, home=None):
        assert token == "good-token"
        return _FakeOperatorSession(jti="jti-1", device_fp="0a1b2c3d4e5f6789", exp=99999)

    monkeypatch.setattr("capauth.pairing.verify_operator_session", fake_verify)

    request = FakeRequest(headers={"x-operator-token": "good-token", "x-sk-actor": "ignored"})
    actor = consent.resolve_consent_actor(request)

    assert actor["verified"] is True
    assert actor["id"] == "device:0a1b2c3d4e5f6789"
    assert actor["session"] == "opsess:jti-1"
    assert actor["role"] == "human"


def test_resolve_consent_actor_falls_back_to_header_on_invalid_session(monkeypatch):
    def fake_verify(token, *, home=None):
        raise Exception("bad/expired token")

    monkeypatch.setattr("capauth.pairing.verify_operator_session", fake_verify)

    request = FakeRequest(headers={"x-operator-token": "bad", "x-sk-actor": "chef@skworld.io"})
    actor = consent.resolve_consent_actor(request)

    assert actor["verified"] is False
    assert actor["id"] == "chef@skworld.io"


def test_resolve_consent_actor_unattributed_when_header_not_canonicalizable():
    """Most existing X-SK-Actor values ("operator", "dashboard") do not match
    the fqid grammar - this is the expected, honest degrade, not a bug."""
    request = FakeRequest(headers={"x-sk-actor": "operator"})
    actor = consent.resolve_consent_actor(request)

    assert actor["id"] == "unattributed"
    assert actor["verified"] is False


def test_resolve_consent_actor_unattributed_with_no_headers():
    request = FakeRequest(headers={})
    actor = consent.resolve_consent_actor(request)

    assert actor == {"id": "unattributed", "role": "human", "verified": False, "session": None}


def test_resolve_consent_actor_never_raises_on_capauth_import_failure(monkeypatch):
    monkeypatch.setattr(
        "capauth.pairing.verify_operator_session",
        lambda token, **kw: (_ for _ in ()).throw(ImportError("no capauth")),
    )
    request = FakeRequest(headers={"x-operator-token": "x"})
    actor = consent.resolve_consent_actor(request)
    assert actor["id"] == "unattributed"


# --------------------------------------------------------------------------- #
# 2. build_consent_event shape
# --------------------------------------------------------------------------- #
def test_build_consent_event_shape():
    actor = {"id": "chef@skworld.io", "role": "human", "verified": True, "session": "opsess:jti-1"}
    decision = {
        "ok": True,
        "reason": "pdp ok",
        "via": "pdp",
        "obligations": [{"kind": "audit", "data": {"event": "authz.decide"}}],
    }

    event = consent.build_consent_event(
        actor=actor,
        capability="agentrun.queue",
        resource_store="cards",
        resource_kind="card",
        resource_id="task-1",
        decision=decision,
        prior="3",
        ts="2026-08-15T00:00:00+00:00",
    )

    assert event["spe"] == "spe1"
    assert event["ts"] == "2026-08-15T00:00:00+00:00"
    assert event["action"] == "consent.granted"
    assert event["actor"]["id"] == "chef@skworld.io"
    assert event["actor"]["session"] == "opsess:jti-1"
    assert event["target"] == {"store": "cards", "kind": "card", "id": "task-1"}
    assert event["prior"] == "3"
    assert event["capability"] == "agentrun.queue"
    assert event["decision"] == {
        "ok": True,
        "reason": "pdp ok",
        "via": "pdp",
        "obligations": decision["obligations"],
    }
    # The sig slot is always present (mandatory from day one, per
    # PROVENANCE_AND_MUTATION_STANDARD.md section 1). Whether it actually
    # carries a signature depends on whether a real capauth identity key is
    # present in this environment (permissive: sign when present, count when
    # not) - see the two dedicated signing tests below for both branches.
    assert "suite_id" in event["sig"]
    assert "value" in event["sig"]


def test_build_consent_event_unsigned_when_no_capauth_key(monkeypatch):
    """Permissive posture, no-key branch: sig.value stays null, never raises."""
    monkeypatch.setattr("skcapstone.fleet.signing.capauth_signer", lambda: None)

    event = consent.build_consent_event(
        actor={"id": "chef@skworld.io", "role": "human", "verified": True, "session": None},
        capability="agentrun.queue",
        resource_store="cards",
        resource_kind="card",
        resource_id="task-1",
        decision={"ok": True, "reason": "loopback-open", "via": "none"},
    )
    assert event["sig"]["value"] is None


def test_build_consent_event_signed_when_capauth_key_present(monkeypatch):
    """Permissive posture, key-present branch: the signer is called over the
    canonical bytes with sig.value blanked, and its output lands in sig.value."""
    calls = []

    def fake_signer(data: bytes) -> str:
        calls.append(data)
        return "fake-signature"

    monkeypatch.setattr("skcapstone.fleet.signing.capauth_signer", lambda: fake_signer)

    event = consent.build_consent_event(
        actor={"id": "chef@skworld.io", "role": "human", "verified": True, "session": None},
        capability="agentrun.queue",
        resource_store="cards",
        resource_kind="card",
        resource_id="task-1",
        decision={"ok": True, "reason": "loopback-open", "via": "none"},
    )
    assert event["sig"]["value"] == "fake-signature"
    assert len(calls) == 1
    # The bytes signed had sig.value BLANKED, not the final signature.
    assert b"fake-signature" not in calls[0]


def test_build_consent_event_defaults_actor_id_to_unattributed_when_missing():
    event = consent.build_consent_event(
        actor={},
        capability="change.validate",
        resource_store="itil.changes",
        resource_kind="change",
        resource_id="chg-1",
        decision={"ok": True, "reason": "loopback-open", "via": "none"},
    )
    assert event["actor"]["id"] == "unattributed"
    assert event["decision"]["obligations"] == []


# --------------------------------------------------------------------------- #
# 3. Store round-trips (no new store - the existing CardStore/ITILManager)
# --------------------------------------------------------------------------- #
def test_record_and_query_card_consent_round_trip(home):
    from skcapstone.card_store import CardCore, CardStore

    store = CardStore(home)
    store.create(CardCore(id="task-1", title="t"))
    actor = {"id": "chef@skworld.io", "role": "human", "verified": True, "session": None}
    decision = {"ok": True, "reason": "pdp ok", "via": "pdp", "obligations": []}

    consent.record_card_consent(
        home, "task-1", actor=actor, capability="agentrun.queue", decision=decision
    )

    events = consent.consent_history_for_card(home, "task-1")
    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == "consent.granted"
    assert ev["actor"]["id"] == "chef@skworld.io"
    assert ev["capability"] == "agentrun.queue"
    assert ev["target"] == {"store": "cards", "kind": "card", "id": "task-1"}
    # Written into the writer's OWN per-writer file, not a shared/mutable one.
    assert (
        store.cards_dir / "task-1" / "events" / f"chef-skworld.io@{consent._HOSTNAME}.jsonl"
    ).exists()


def test_record_card_consent_never_blocks_on_unattributed_actor(home):
    """Even an unresolved identity still gets a durable event - "unattributed"
    is honest and recorded, not a reason to skip persisting consent."""
    from skcapstone.card_store import CardCore, CardStore

    CardStore(home).create(CardCore(id="task-2", title="t"))
    actor = {"id": "unattributed", "role": "human", "verified": False, "session": None}
    decision = {"ok": True, "reason": "loopback-open", "via": "none", "obligations": []}

    consent.record_card_consent(
        home, "task-2", actor=actor, capability="agentrun.queue", decision=decision
    )

    events = consent.consent_history_for_card(home, "task-2")
    assert len(events) == 1
    assert events[0]["actor"]["id"] == "unattributed"


def test_record_and_query_change_consent_round_trip(home):
    from skcoord.itil import ITILManager

    mgr = ITILManager(home)
    chg = mgr.propose_change(title="t", change_type="normal", managed_by="lumina")
    actor = {"id": "chef@skworld.io", "role": "human", "verified": True, "session": None}
    decision = {"ok": True, "reason": "pdp ok", "via": "pdp", "obligations": []}

    consent.record_change_consent(
        mgr, chg.id, actor=actor, capability="change.validate", decision=decision
    )

    events = consent.consent_history_for_change(mgr, chg.id)
    assert len(events) == 1
    ev = events[0]
    assert ev["kind"] == "consent.granted"
    assert ev["action"] == "consent.granted"
    assert ev["actor"]["id"] == "chef@skworld.io"
    assert ev["capability"] == "change.validate"
    assert ev["target"] == {"store": "itil.changes", "kind": "change", "id": chg.id}


# --------------------------------------------------------------------------- #
# 4. Real PEPs persist consent.granted
# --------------------------------------------------------------------------- #
def test_queue_ai_pep_persists_consent_granted(home, monkeypatch):
    from skdashboard.dashboard import create_app

    def _fake_request_run(home, card_id, instruction, agent="lumina", mode="propose", requester="operator"):
        return {"ok": True, "run_id": "run-fake", "card_id": card_id, "state": "queued"}

    monkeypatch.setattr("skcapstone.agent_run.request_run", _fake_request_run)

    app = create_app(home)
    handler = _route_endpoint(app, "/api/card/{card_id}/queue-ai")
    request = FakeRequest(
        headers={"x-sk-actor": "chef@skworld.io"},
        path_params={"card_id": "task-42"},
        json_body={"instruction": "investigate", "mode": "propose"},
    )
    response = _call(handler, request)
    assert response.status_code == 200

    events = consent.consent_history_for_card(home, "task-42")
    assert len(events) == 1
    assert events[0]["actor"]["id"] == "chef@skworld.io"
    assert events[0]["capability"] == "agentrun.queue"
    assert events[0]["decision"]["via"] == "none"  # dev loopback-open


def test_queue_ai_pep_does_not_persist_consent_on_deny(home, monkeypatch):
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "sekrit")
    from skdashboard.dashboard import create_app

    app = create_app(home)
    handler = _route_endpoint(app, "/api/card/{card_id}/queue-ai")
    request = FakeRequest(
        headers={"x-sk-capability": "wrong"},
        path_params={"card_id": "task-43"},
        json_body={"instruction": "x", "mode": "propose"},
    )
    response = _call(handler, request)
    assert response.status_code == 403

    assert consent.consent_history_for_card(home, "task-43") == []


@pytest.mark.parametrize(
    "path,capability",
    [
        ("/api/change/{id}/schedule", "change.schedule"),
        ("/api/change/{id}/arm", "change.deploy"),
    ],
)
def test_change_peps_persist_consent_granted(home, path, capability):
    from skcoord.itil import ITILManager

    from skdashboard.dashboard import create_app

    mgr = ITILManager(home)
    chg = mgr.propose_change(title="seeded", change_type="standard", managed_by="lumina")

    app = create_app(home)
    handler = _route_endpoint(app, path)
    body = {"asap": True} if "schedule" in path else {}
    request = FakeRequest(
        headers={"x-sk-actor": "chef@skworld.io"},
        path_params={"id": chg.id},
        json_body=body,
    )
    response = _call(handler, request)
    assert response.status_code == 200

    events = consent.consent_history_for_change(mgr, chg.id)
    assert len(events) == 1
    assert events[0]["actor"]["id"] == "chef@skworld.io"
    assert events[0]["capability"] == capability


def test_validate_pep_persists_consent_granted(home, monkeypatch):
    """/validate has an extra precondition (a prepared_pr) the other two
    front doors do not, so it gets its own seeding rather than sharing the
    schedule/arm parametrization above."""
    from skcoord.itil import ITILManager

    from skdashboard.dashboard import create_app

    monkeypatch.setattr(
        "skdashboard.dashboard._gh_pr_checks",
        lambda pr_url: {"started": False, "passed": False, "checks": [], "error": None},
    )
    monkeypatch.setattr("skdashboard.dashboard._gh_trigger_checks", lambda *a, **k: False)
    monkeypatch.setattr("skdashboard.dashboard._gh_pr_head_sha", lambda *a, **k: None)

    mgr = ITILManager(home)
    chg = mgr.propose_change(title="seeded", change_type="normal", managed_by="lumina")
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "lumina",
        "pr_link",
        url="https://github.com/smilinTux/skdashboard/pull/1",
        branch="chg/seeded",
        run_id="run-1",
        head_sha="deadbeef",
    )

    app = create_app(home)
    handler = _route_endpoint(app, "/api/change/{id}/validate")
    request = FakeRequest(
        headers={"x-sk-actor": "chef@skworld.io"}, path_params={"id": chg.id}, json_body={}
    )
    response = _call(handler, request)
    assert response.status_code == 200

    events = consent.consent_history_for_change(mgr, chg.id)
    assert len(events) == 1
    assert events[0]["actor"]["id"] == "chef@skworld.io"
    assert events[0]["capability"] == "change.validate"


def test_change_consent_query_route_answers_who_and_when(home):
    from skcoord.itil import ITILManager

    from skdashboard.dashboard import create_app

    mgr = ITILManager(home)
    chg = mgr.propose_change(title="seeded", change_type="standard", managed_by="lumina")

    app = create_app(home)
    arm_handler = _route_endpoint(app, "/api/change/{id}/arm")
    _call(
        arm_handler,
        FakeRequest(headers={"x-sk-actor": "chef@skworld.io"}, path_params={"id": chg.id}),
    )

    consent_handler = _route_endpoint(app, "/api/change/{id}/consent")
    response = _call(consent_handler, FakeRequest(path_params={"id": chg.id}))
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["id"] == chg.id
    assert len(body["events"]) == 1
    assert body["events"][0]["actor"]["id"] == "chef@skworld.io"
    assert "ts" in body["events"][0]


def test_card_consent_query_route_answers_who_and_when(home, monkeypatch):
    from skdashboard.dashboard import create_app

    monkeypatch.setattr(
        "skcapstone.agent_run.request_run",
        lambda home, card_id, instruction, agent="lumina", mode="propose", requester="operator": {
            "ok": True,
            "run_id": "run-x",
            "card_id": card_id,
            "state": "queued",
        },
    )
    app = create_app(home)
    queue_handler = _route_endpoint(app, "/api/card/{card_id}/queue-ai")
    _call(
        queue_handler,
        FakeRequest(
            headers={"x-sk-actor": "chef@skworld.io"},
            path_params={"card_id": "task-99"},
            json_body={"instruction": "x"},
        ),
    )

    consent_handler = _route_endpoint(app, "/api/card/{card_id}/consent")
    response = _call(consent_handler, FakeRequest(path_params={"card_id": "task-99"}))
    body = json.loads(response.body)
    assert body["id"] == "task-99"
    assert len(body["events"]) == 1
    assert body["events"][0]["actor"]["id"] == "chef@skworld.io"


# --------------------------------------------------------------------------- #
# 5. Fold determinism (acceptance criterion 5)
# --------------------------------------------------------------------------- #
def test_consent_events_fold_converges_regardless_of_writer_arrival_order(tmp_path):
    """Two simulated writers (the CardStore's per-writer-file shape - the
    exact mechanism that makes concurrent Syncthing appends merge instead of
    conflict) append consent.granted events in OPPOSITE arrival order on two
    independent "nodes". The folded read must converge on the identical,
    chronologically-ordered result on both, regardless of which writer's
    file the filesystem happened to see first.
    """
    from skcapstone.card_store import CardCore, CardStore

    card_id = "task-converge"
    actor_a = {"id": "device:aaaaaaaaaaaaaaaa", "role": "human", "verified": True, "session": None}
    actor_b = {"id": "device:bbbbbbbbbbbbbbbb", "role": "human", "verified": True, "session": None}
    decision = {"ok": True, "reason": "loopback-open", "via": "none", "obligations": []}

    # Writer B's event is logically EARLIER than writer A's, so a correct
    # fold must order it first regardless of which writer physically landed
    # (or synced) first - proving convergence depends on event CONTENT
    # (ts, writer, seq), never on arrival order.
    event_a = consent.build_consent_event(
        actor=actor_a,
        capability="agentrun.queue",
        resource_store="cards",
        resource_kind="card",
        resource_id=card_id,
        decision=decision,
        ts="2026-08-15T10:00:00+00:00",
    )
    event_b = consent.build_consent_event(
        actor=actor_b,
        capability="agentrun.queue",
        resource_store="cards",
        resource_kind="card",
        resource_id=card_id,
        decision=decision,
        ts="2026-08-15T09:00:00+00:00",
    )

    def _build_store(home, write_order):
        store = CardStore(home)
        store.create(
            CardCore(id=card_id, title="converge test", created_at="2026-08-15T08:00:00+00:00")
        )
        for actor, event in write_order:
            payload = {k: v for k, v in event.items() if k != "action"}
            store.append_event(card_id, consent.CONSENT_ACTION, actor["id"], **payload)
        return store

    # Node 1: writer A's file lands (syncs) before writer B's.
    store1 = _build_store(tmp_path / "node1", [(actor_a, event_a), (actor_b, event_b)])
    # Node 2: the opposite arrival order.
    store2 = _build_store(tmp_path / "node2", [(actor_b, event_b), (actor_a, event_a)])

    def _consent_events(store):
        return [e for e in store._read_events(card_id) if e.get("action") == consent.CONSENT_ACTION]

    def _fingerprint(events):
        return [(e["actor"]["id"], e["ts"]) for e in events]

    events1 = _consent_events(store1)
    events2 = _consent_events(store2)

    assert _fingerprint(events1) == _fingerprint(events2)
    # Chronological, not arrival-order: B (09:00) folds before A (10:00) on
    # BOTH nodes, even though node 1 physically wrote A first.
    assert _fingerprint(events1) == [
        (actor_b["id"], "2026-08-15T09:00:00+00:00"),
        (actor_a["id"], "2026-08-15T10:00:00+00:00"),
    ]

    # The whole-card fold (consent.granted is an unmapped action for Card's
    # own fold - additive and safe, per PROVENANCE_AND_MUTATION_STANDARD.md
    # section 1: "a new envelope field never breaks an old reader") also
    # converges: same status, same final updated_at on both nodes.
    card1 = store1.fold(card_id)
    card2 = store2.fold(card_id)
    assert card1.status == card2.status
    assert card1.updated_at == card2.updated_at == "2026-08-15T10:00:00+00:00"


def test_change_consent_events_fold_converges_regardless_of_writer_arrival_order(tmp_path):
    """Same convergence property, proven against the ITILManager-backed
    changes store (the change-management PEPs' own object store)."""
    from skcoord.itil import ITILManager

    actor_a = {"id": "device:aaaaaaaaaaaaaaaa", "role": "human", "verified": True, "session": None}
    actor_b = {"id": "device:bbbbbbbbbbbbbbbb", "role": "human", "verified": True, "session": None}
    decision = {"ok": True, "reason": "loopback-open", "via": "none", "obligations": []}

    mgr_seed = ITILManager(tmp_path / "seed")
    chg = mgr_seed.propose_change(title="converge", change_type="normal", managed_by="lumina")
    change_id = chg.id
    core = (mgr_seed.changes_dir / change_id / "core.json").read_text(encoding="utf-8")

    event_a = consent.build_consent_event(
        actor=actor_a,
        capability="change.validate",
        resource_store="itil.changes",
        resource_kind="change",
        resource_id=change_id,
        decision=decision,
        ts="2026-08-15T10:00:00+00:00",
    )
    event_b = consent.build_consent_event(
        actor=actor_b,
        capability="change.validate",
        resource_store="itil.changes",
        resource_kind="change",
        resource_id=change_id,
        decision=decision,
        ts="2026-08-15T09:00:00+00:00",
    )

    def _build_mgr(home, write_order):
        mgr = ITILManager(home)
        mgr.ensure_dirs()
        core_path = mgr.changes_dir / change_id / "core.json"
        core_path.parent.mkdir(parents=True, exist_ok=True)
        core_path.write_text(core, encoding="utf-8")
        for actor, event in write_order:
            mgr._append_event(mgr.changes_dir, change_id, actor["id"], consent.CONSENT_ACTION, **event)
        return mgr

    mgr1 = _build_mgr(tmp_path / "node1", [(actor_a, event_a), (actor_b, event_b)])
    mgr2 = _build_mgr(tmp_path / "node2", [(actor_b, event_b), (actor_a, event_a)])

    def _fingerprint(mgr):
        events = [
            e
            for e in mgr._read_events(mgr.changes_dir, change_id)
            if e.get("kind") == consent.CONSENT_ACTION
        ]
        return [(e["actor"]["id"], e["ts"]) for e in events]

    fp1 = _fingerprint(mgr1)
    fp2 = _fingerprint(mgr2)
    assert fp1 == fp2
    assert fp1 == [
        (actor_b["id"], "2026-08-15T09:00:00+00:00"),
        (actor_a["id"], "2026-08-15T10:00:00+00:00"),
    ]
