"""Supersession of finished reviews whose sealed reviewer claim was released."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_builder, review_retire
from skcapstone.fleet.paths import FleetPaths

CARD, NODE = "add19bf8", "node-chiap03"
OWNER, CLAIM = "pi-seraph-chiap03-add19bf8", "4" * 32
REQUEST_ID = "e" * 64


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    path.chmod(0o600)


class FakeStore:
    def __init__(self, events, card):
        self.events, self.card = events, card

    def _read_events(self, card_id):
        return list(self.events)

    def _legacy_events(self, card_id):
        return []

    def fold(self, card_id):
        return self.card

    def append_event(self, card_id, action, writer, **fields):
        self.events.append({"action": action, "writer": writer, **fields})


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    request = {"request_id": REQUEST_ID, "card_id": CARD, "node": NODE}
    status = {
        "request_id": REQUEST_ID,
        "card_id": CARD,
        "work_kind": "review",
        "state": "awaiting-review-acceptance",
    }
    _write(paths.root / "dispatch" / NODE / f"{CARD}.json", request)
    _write(paths.status_path(NODE, "dispatch", CARD), status)
    _write(home / "evidence/production-review-exits" / f"{CARD}-{CLAIM}.json", {"ok": 1})
    _write(
        home / "evidence/production-acceptance" / f"{CARD}-{CLAIM}" / "context.json",
        {"review": {"card": CARD, "owner": OWNER, "claim": CLAIM}},
    )
    events = [
        {
            "action": "remote_review_offer",
            "request_id": REQUEST_ID,
            "request_sha256": production_builder.digest(request),
        },
        {
            "action": "review_assignment_launch",
            "claim_revision": CLAIM,
            "launched": True,
            "execution": {"request_id": REQUEST_ID, "node": NODE},
        },
        {"action": "release_claim", "released_owner": OWNER, "expected_claim_revision": CLAIM},
    ]
    store = FakeStore(events, SimpleNamespace(owner=None, meta={}, archived=False))
    monkeypatch.setattr(review_retire, "CardStore", lambda home: store)
    monkeypatch.setattr(review_retire, "card_mutation_lock", lambda *args: nullcontext())
    return SimpleNamespace(home=home, paths=paths, store=store, request=request)


def test_released_reviewer_generation_is_superseded_and_becomes_historical(world):
    results = review_retire.supersede_released_reviews(world.paths, world.home)
    assert results == [{"card": CARD, "claim": CLAIM, "state": "superseded"}]
    assert not (world.paths.root / "dispatch" / NODE / f"{CARD}.json").exists()
    assert not world.paths.status_path(NODE, "dispatch", CARD).exists()
    (event,) = [e for e in world.store.events if e["action"] == "remote_review_supersede"]
    assert event["schema"] == review_retire.SUPERSEDE_SCHEMA
    assert event["released_owner"] == OWNER and event["released_claim"] == CLAIM
    retired = review_retire.retired_offers(world.home, CARD, world.store.events)
    assert REQUEST_ID in retired
    # A second pass is a no-op: the generation is already retired.
    assert review_retire.supersede_released_reviews(world.paths, world.home) == []


def test_reviewer_still_holding_its_claim_is_never_superseded(world):
    held = {"_claim_revision": CLAIM}
    world.store.card = SimpleNamespace(owner=OWNER, meta=held, archived=False)
    assert review_retire.supersede_released_reviews(world.paths, world.home) == []
    assert (world.paths.root / "dispatch" / NODE / f"{CARD}.json").exists()


def test_no_native_release_means_no_supersession(world):
    world.store.events[:] = [e for e in world.store.events if e["action"] != "release_claim"]
    assert review_retire.supersede_released_reviews(world.paths, world.home) == []


def test_a_running_review_status_is_never_superseded(world):
    _write(
        world.paths.status_path(NODE, "dispatch", CARD),
        {"request_id": REQUEST_ID, "card_id": CARD, "work_kind": "review", "state": "running"},
    )
    assert review_retire.supersede_released_reviews(world.paths, world.home) == []
    assert world.paths.status_path(NODE, "dispatch", CARD).exists()


def test_tampered_receipt_is_not_recognized(world):
    review_retire.supersede_released_reviews(world.paths, world.home)
    receipt = review_retire._supersession_receipt(world.home, CARD, REQUEST_ID)
    archived = receipt.parent / "context.json"
    archived.chmod(0o600)
    archived.write_text("{}")
    assert REQUEST_ID not in review_retire.retired_offers(world.home, CARD, world.store.events)
