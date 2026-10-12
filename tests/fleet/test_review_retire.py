"""Failed remote review retirement preserves exact evidence before reoffer."""

import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import review_dispatch, review_retire
from skcapstone.fleet.paths import FleetPaths


def test_failed_review_retirement_requires_proof_and_archives_before_reoffer(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node, owner = "c60a542e", "node-chiap03", "pi-seraph-chiap03-c60a542e"
    request_id = "a" * 64
    request = {
        "schema": "skfleet.builder-dispatch/v2",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "reviewer": owner,
    }
    status = {
        "schema": "skfleet.builder-dispatch-status/v1",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "state": "running",
        "owner": owner,
        "claim_revision": "b" * 32,
        "execution": {
            "host": "chiap03",
            "unit": "skfleet-worker-glm-c60a542e.service",
            "invocation": "c" * 32,
            "request_id": request_id,
            "request_sha256": review_retire.production_builder.digest(request),
        },
    }
    request_path = review_retire.dispatch.request_path(paths, node, card)
    status_path = review_retire.dispatch.status_path(paths, node, card)
    review_retire.source_bundle._once(request_path, json.dumps(request).encode())
    review_retire.source_bundle._once(status_path, json.dumps(status).encode())
    exit_path = home / "evidence/worker-exits" / (card + "-" + "d" * 16 + ".json")
    review_retire.source_bundle._once(
        exit_path,
        json.dumps(
            {
                "card_id": card,
                "owner": owner,
                "claim_revision": "b" * 32,
                "host": "chiap03",
                "child_exit_code": 1,
            }
        ).encode(),
    )
    events = [
        {
            "action": "remote_review_offer",
            "request_id": request_id,
            "request_sha256": review_retire.production_builder.digest(request),
        },
        {
            "action": "review_assignment_launch",
            "recommendation_id": request_id,
            "claim_revision": "b" * 32,
            "execution": status["execution"],
            "writer": owner,
            "launched": True,
        },
    ]

    class FakeStore:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return SimpleNamespace(archived=False, meta={}, labels=[])

        def _read_events(self, _card):
            return events

        def append_event(self, _card, action, writer, **payload):
            events.append(dict(action=action, writer=writer, **payload))

    monkeypatch.setattr(review_retire, "CardStore", FakeStore)
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review_retire, "governed_review_assignment_ready", lambda *_: True)
    monkeypatch.setattr(review_retire, "review_state_revision", lambda *_: "e" * 64)
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(
        review_retire.production_builder, "node_binding", lambda *_: {"host": "chiap03"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        review_retire, "unit_terminal", lambda *_args, **_kw: {"ActiveState": "inactive"}
    )
    kwargs = dict(
        request_sha256=review_retire.source_bundle._sha(request_path.read_bytes()),
        status_sha256=review_retire.source_bundle._sha(status_path.read_bytes()),
        card_sha256="e" * 64,
        actor="jarvis",
        reason="failed before review custody",
    )
    with pytest.raises(ValueError, match="release proof missing"):
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert request_path.exists() and status_path.exists()
    events.append(
        {"action": "release_claim", "released_owner": owner, "expected_claim_revision": "b" * 32}
    )
    with pytest.raises(ValueError, match="hash changed"):
        review_retire.retire(
            paths,
            home,
            node,
            card,
            apply=True,
            **{**kwargs, "request_sha256": "f" * 64},
        )
    failed_exit = exit_path.read_bytes()
    exit_value = json.loads(failed_exit)
    exit_value["child_exit_code"] = 0
    exit_path.write_text(json.dumps(exit_value))
    with pytest.raises(ValueError, match="failed worker exit required"):
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    exit_path.write_bytes(failed_exit)

    def live_unit(*_args, **_kwargs):
        raise ValueError("exact managed worker death unproven")

    monkeypatch.setattr(review_retire, "unit_terminal", live_unit)
    with pytest.raises(ValueError, match="death unproven"):
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert request_path.exists() and status_path.exists()
    monkeypatch.setattr(
        review_retire, "unit_terminal", lambda *_args, **_kw: {"ActiveState": "inactive"}
    )
    assert (
        review_retire.retire(paths, home, node, card, **kwargs)["state"] == "qualified-check-only"
    )
    assert request_path.exists() and status_path.exists()
    result = review_retire.retire(paths, home, node, card, apply=True, **kwargs)
    assert result["state"] == "retired"
    assert not request_path.exists() and not status_path.exists()
    assert review_retire.retired_offers(home, card, events) == {request_id}
    assert (
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)["state"]
        == "already-retired"
    )
    # Interrupted cleanup retains its exact event and can remove remaining pointers.
    archive = Path(result["receipt"]).parent
    request_path.write_bytes((archive / "request.json").read_bytes())
    request_path.chmod(0o600)
    assert (
        review_retire.retire(paths, home, node, card, apply=True, **kwargs)["state"]
        == "already-retired"
    )
    assert not request_path.exists()
    (archive / "status.json").write_bytes(b"{}")
    assert review_retire.retired_offers(home, card, events) == set()


def test_retirement_without_archive_cannot_release_historical_offer(tmp_path):
    card, request_id = "c60a542e", "a" * 64
    events = [
        {"action": "remote_review_offer", "request_id": request_id, "request_sha256": "b" * 64},
        {
            "action": "remote_review_retire",
            "writer": "jarvis",
            "actor": "jarvis",
            "schema": review_retire.SCHEMA,
            "request_id": request_id,
            "request_sha256": "b" * 64,
            "offer_request_sha256": "e" * 64,
            "status_sha256": "c" * 64,
            "receipt_sha256": "d" * 64,
        },
    ]
    assert review_retire.retired_offers(tmp_path, card, events) == set()


def test_prestart_retirement_requires_exact_release_and_absent_native_start(tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node = "c60a542e", "node-chiap03"
    owner, claim, request_id = "pi-seraph-chiap03-c60a542e", "b" * 32, "a" * 64
    source = {"card": "3f9eaa96", "head": "c" * 40, "claim": "d" * 32}
    core = dict(
        title="[REVIEW] exact source",
        description="review exact candidate",
        acceptance_criteria=["verify exact bytes"],
        dependencies=[],
        links={"repository": "smilinTux/sklegal"},
        labels=["review", "source-only", "seat-seraph"],
    )
    request = {
        "schema": "skfleet.builder-dispatch/v2",
        "work_kind": "review",
        "card_id": card,
        "node": node,
        "request_id": request_id,
        "reviewer": owner,
        "review_revision": "e" * 64,
        "source": source,
        "criteria_sha256": review_retire.production_builder.digest(core),
        "production": {"host": "chiap03", "family": "codex"},
        "labels": core["labels"],
    }
    request_path = review_retire.dispatch.request_path(paths, node, card)
    status_path = review_retire.dispatch.status_path(paths, node, card)
    review_retire.source_bundle._once(request_path, json.dumps(request).encode())
    raw_request = request_path.read_bytes()
    offer_digest = review_retire.production_builder.digest(request)
    events = [
        {
            "action": "remote_review_offer",
            "request_id": request_id,
            "request_sha256": offer_digest,
        },
        {
            "action": "release_claim",
            "writer": "jarvis",
            "released_owner": owner,
            "expected_claim_revision": claim,
        },
    ]
    current = SimpleNamespace(
        archived=False,
        owner=None,
        meta={},
        **core,
    )
    current.model_dump = lambda **_kwargs: core

    class FakeStore:
        def __init__(self, _home):
            pass

        def fold(self, _card):
            return current

        def _read_events(self, _card):
            return events

        def append_event(self, _card, action, writer, **payload):
            events.append(dict(action=action, writer=writer, **payload))

    monkeypatch.setattr(review_retire, "CardStore", FakeStore)
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    monkeypatch.setattr(review_retire, "governed_review_assignment_ready", lambda *_: True)
    monkeypatch.setattr(review_retire, "review_state_revision", lambda *_: "f" * 64)
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(
        review_retire.production_builder, "node_binding", lambda *_args: {"host": "chiap03"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(
        review_retire,
        "_prestart_state",
        lambda *_args: {
            "unit": "skfleet-worker-codex-c60a542e.service",
            "unit_state": {"LoadState": "not-found"},
            "matching_sessions": 0,
        },
    )
    monkeypatch.setattr(review_dispatch, "_source", lambda *_args: source)
    kwargs = dict(
        request_sha256=review_retire.source_bundle._sha(raw_request),
        card_sha256="f" * 64,
        previous_owner=owner,
        previous_claim_revision=claim,
        actor="jarvis",
        reason="retained sealed offer never reached native launch",
    )
    assert (
        review_retire.retire_prestart(paths, home, node, card, **kwargs)["state"]
        == "qualified-check-only"
    )
    assert request_path.exists() and not status_path.exists()
    result = review_retire.retire_prestart(paths, home, node, card, apply=True, **kwargs)
    assert result["state"] == "retired-prestart"
    assert not request_path.exists()
    assert review_retire.retired_offers(home, card, events) == {request_id}


def test_prestart_retirement_refuses_when_status_or_launch_exists(tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    card, node = "c60a542e", "node-chiap03"
    status = review_retire.dispatch.status_path(paths, node, card)
    status.parent.mkdir(parents=True)
    status.write_text("{}")
    monkeypatch.setattr(
        review_retire.production_builder, "policy", lambda: {"authority_host": "chiap08"}
    )
    monkeypatch.setattr(review_retire.socket, "gethostname", lambda: "chiap08")
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *_: nullcontext())
    with pytest.raises(ValueError, match="status already exists"):
        review_retire.retire_prestart(
            paths,
            home,
            node,
            card,
            request_sha256="a" * 64,
            card_sha256="b" * 64,
            previous_owner="pi-seraph-chiap03-c60a542e",
            previous_claim_revision="c" * 32,
            actor="jarvis",
            reason="bounded test",
        )


def test_prestart_retirement_refuses_matching_resource_intent(tmp_path, monkeypatch):
    home = tmp_path / "home"
    reservation = home / "fleet/resource-admission/chiap03" / ("a" * 64)
    reservation.mkdir(parents=True)
    (reservation / "intent.json").write_text("{}")
    intent = {
        "binding": {
            "card_id": "c60a542e",
            "owner": "pi-seraph-chiap03-c60a542e",
            "claim_revision": "b" * 32,
            "request_id": "c" * 64,
        }
    }
    from skcapstone.fleet import production_admission

    monkeypatch.setattr(production_admission, "read_json", lambda _path: intent)
    monkeypatch.setattr(production_admission, "_reservation_id", lambda _intent: "a" * 64)
    with pytest.raises(ValueError, match="matching resource admission intent exists"):
        review_retire._admission_inventory(
            home,
            "chiap03",
            "c60a542e",
            "pi-seraph-chiap03-c60a542e",
            "b" * 32,
            "c" * 64,
        )


def test_prestart_inventory_ignores_private_admission_lock(tmp_path):
    root = tmp_path / "home/fleet/resource-admission/chiap03"
    root.mkdir(parents=True)
    lock = root / ".lock"
    lock.touch(mode=0o600)

    digest = review_retire._admission_inventory(
        tmp_path / "home",
        "chiap03",
        "c60a542e",
        "pi-seraph-chiap03-c60a542e",
        "b" * 32,
        "c" * 64,
    )

    assert digest == review_retire.source_bundle._sha(b"[]")


def test_prestart_inventory_rejects_unsafe_admission_lock(tmp_path):
    root = tmp_path / "home/fleet/resource-admission/chiap03"
    root.mkdir(parents=True)
    target = tmp_path / "lock-target"
    target.touch(mode=0o600)
    (root / ".lock").symlink_to(target)

    with pytest.raises(ValueError, match="resource admission lock is unsafe"):
        review_retire._admission_inventory(
            tmp_path / "home",
            "chiap03",
            "c60a542e",
            "pi-seraph-chiap03-c60a542e",
            "b" * 32,
            "c" * 64,
        )
