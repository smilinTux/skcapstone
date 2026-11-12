"""A finished remote review keeps its claim until production acceptance consumes it."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_custody as custody
from skcapstone.fleet.paths import FleetPaths

CARD, OWNER, CLAIM, TOKEN = "add19bf8", "pi-seraph-chiap03-add19bf8", "c" * 32, "e" * 64


def _setup(
    tmp_path,
    monkeypatch,
    *,
    state="awaiting-review-acceptance",
    owner=OWNER,
    claim=CLAIM,
    status_claim=CLAIM,
):
    paths = FleetPaths(tmp_path / "fleet")
    status = paths.status_path("node-chiap03", "dispatch", CARD)
    status.parent.mkdir(parents=True)
    status.write_text(
        json.dumps(
            {
                "card_id": CARD,
                "work_kind": "review",
                "request_id": TOKEN,
                "owner": OWNER,
                "claim_revision": status_claim,
                "state": state,
            }
        )
    )
    events = [
        {
            "action": "review_assignment_launch",
            "schema": "skfleet.review-assignment-launch/v3",
            "writer": OWNER,
            "reviewer": OWNER,
            "claim_revision": CLAIM,
            "launched": True,
            "execution": {"node": "node-chiap03", "request_id": TOKEN},
        }
    ]
    card = SimpleNamespace(archived=False, owner=owner, meta={"_claim_revision": claim})
    store = SimpleNamespace(fold=lambda _cid: card, _read_events=lambda _cid: events)
    monkeypatch.setattr(custody, "_read", lambda path, _limit: path.read_bytes())
    return paths, store


def test_finished_review_awaiting_acceptance_is_preserved(tmp_path, monkeypatch):
    paths, store = _setup(tmp_path, monkeypatch)
    assert custody.retains_review_custody(
        tmp_path, CARD, OWNER, CLAIM, fleet_paths=paths, store=store
    )


@pytest.mark.parametrize(
    "change",
    [
        {"state": "review-fail"},
        {"owner": "someone-else"},
        {"claim": "d" * 32},
        {"status_claim": "d" * 32},
    ],
)
def test_other_states_or_generations_are_not_preserved(tmp_path, monkeypatch, change):
    paths, store = _setup(tmp_path, monkeypatch, **change)
    assert not custody.retains_review_custody(
        tmp_path, CARD, OWNER, CLAIM, fleet_paths=paths, store=store
    )
