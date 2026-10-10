"""Automatic recovery of remote review generations whose worker provably failed."""

import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import review_retire
from skcapstone.fleet.paths import FleetPaths

CARD, NODE, OWNER, CLAIM = "ffd28b33", "node-chiap04", "pi-seraph-chiap04-ffd28b33", "c" * 32


def _tree(tmp_path, *, exits=(1,), state="running", packet=None):
    home = tmp_path / "home"
    paths = FleetPaths(home / "fleet")
    status = {
        "card_id": CARD,
        "work_kind": "review",
        "state": state,
        "owner": OWNER,
        "claim_revision": CLAIM,
        "review_packet": packet,
        "terminal": None,
    }
    status_file = paths.root / "status" / NODE / "dispatch" / f"{CARD}.json"
    status_file.parent.mkdir(parents=True)
    status_file.write_text(json.dumps(status))
    status_file.chmod(0o600)
    request_file = paths.root / "dispatch" / NODE / f"{CARD}.json"
    request_file.parent.mkdir(parents=True)
    request_file.write_text(json.dumps({"card_id": CARD, "node": NODE}))
    request_file.chmod(0o600)
    exits_dir = home / "evidence/worker-exits"
    exits_dir.mkdir(parents=True)
    for index, code in enumerate(exits):
        exit_file = exits_dir / f"{CARD}-{index:016x}.json"
        exit_file.write_text(
            json.dumps(
                {
                    "card_id": CARD,
                    "owner": OWNER,
                    "claim_revision": CLAIM,
                    "child_exit_code": code,
                }
            )
        )
        exit_file.chmod(0o600)
    return home, paths


@pytest.fixture
def harness(monkeypatch):
    calls = SimpleNamespace(
        releases=[],
        retires=[],
        folded=SimpleNamespace(owner=OWNER, meta={"_claim_revision": CLAIM}),
    )

    def release(_self, owner, card, **kwargs):
        calls.releases.append((owner, card, kwargs))
        calls.folded = SimpleNamespace(owner=None, meta={})
        return True

    def retire(paths, home, node, card, **kwargs):
        calls.retires.append((node, card, kwargs))
        return {"state": "retired"}

    monkeypatch.setattr("skcapstone.coordination.Board.release_claim", release)
    monkeypatch.setattr(review_retire.CardStore, "fold", lambda *args: calls.folded)
    monkeypatch.setattr(review_retire, "review_state_revision", lambda card: "d" * 64)
    monkeypatch.setattr(review_retire, "retire", retire)
    return calls


def test_failed_reviewer_is_released_then_retired(tmp_path, harness):
    home, paths = _tree(tmp_path)
    results = review_retire.recover_failed_generations(paths, home)
    assert results == [{"card": CARD, "node": NODE, "state": "retired"}]
    expected = {"actor": OWNER, "expected_claim_revision": CLAIM, "abandon_reason": "error"}
    assert harness.releases == [(OWNER, CARD, expected)]
    node, card, kwargs = harness.retires[0]
    assert (node, card) == (NODE, CARD)
    assert kwargs["apply"] is True
    assert kwargs["card_sha256"] == "d" * 64


def test_already_released_claim_is_only_retired(tmp_path, harness):
    home, paths = _tree(tmp_path)
    harness.folded = SimpleNamespace(owner=None, meta={})
    review_retire.recover_failed_generations(paths, home)
    assert harness.releases == []
    assert len(harness.retires) == 1


@pytest.mark.parametrize(
    "tree",
    [
        {"exits": ()},
        {"exits": (0,)},
        {"exits": (1, 2)},
        {"state": "awaiting-review-acceptance"},
        {"packet": {"manifest": "present"}},
    ],
    ids=["no-exit", "clean-exit", "two-exits", "terminal-state", "has-packet"],
)
def test_no_proof_of_failure_touches_nothing(tmp_path, harness, tree):
    home, paths = _tree(tmp_path, **tree)
    assert review_retire.recover_failed_generations(paths, home) == []
    assert harness.releases == []
    assert harness.retires == []


def test_retire_refusal_is_reported_not_raised(tmp_path, harness, monkeypatch):
    home, paths = _tree(tmp_path)

    def refuse(*args, **kwargs):
        raise ValueError("exact managed worker death unproven")

    monkeypatch.setattr(review_retire, "retire", refuse)
    results = review_retire.recover_failed_generations(paths, home)
    assert results == [
        {
            "card": CARD,
            "node": NODE,
            "state": "refused",
            "reason": "exact managed worker death unproven",
        }
    ]
