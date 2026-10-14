"""Shared exact claim custody decisions, independent of caller policy."""

import pytest

from skcapstone.fleet.source_custody import CustodyState, inspect_custody
from skcapstone.seraph_review_cardstore import _binding, _latest_outcome
from tests.test_guarded_review_work import prepared


@pytest.mark.parametrize(
    "change,expected",
    [
        ("held", "HELD"),
        ("release", "RELEASED_CLEAN"),
        ("claim", "CHANGED"),
        ("assign", "CHANGED"),
        ("unassign", "CHANGED"),
        ("complete", "CHANGED"),
        ("void", "CHANGED"),
        ("wrong-owner", "CHANGED"),
        ("wrong-claim", "CHANGED"),
        ("tree", "CHANGED"),
        ("repository", "CHANGED"),
        ("outcome", "CHANGED"),
    ],
)
def test_custody_decision_table(tmp_path, change, expected):
    store, card, owner, _, _ = prepared(tmp_path)
    row = store.fold(card)
    claim = row.meta["_claim_revision"]
    outcome = _latest_outcome(store, card)
    source = dict(
        card=card,
        owner=owner,
        claim=claim,
        repository=_binding(row, "repository"),
        head=outcome["candidate_commit"],
        tree=outcome["candidate_tree"],
        ref=outcome["candidate_ref"],
        evidence_sha256=outcome["candidate_sha256"],
    )
    original = row.model_dump()
    if change != "held":
        store.append_event(
            card,
            "release_claim",
            "sweeper",
            released_owner=owner,
            expected_claim_revision=claim,
            abandon_reason="error",
        )
    if change in {"claim", "assign", "unassign", "complete", "void"}:
        store.append_event(card, change, "other", owner="other", claim_revision="d" * 32)
    elif change in {"tree", "repository", "outcome"}:
        store.append_event(
            card,
            "link",
            "other",
            link_key="verdict" if change == "outcome" else change,
            link_value="BLOCKED" if change == "outcome" else "f" * 40,
        )
    proof = inspect_custody(
        tmp_path,
        card,
        "other" if change == "wrong-owner" else owner,
        "d" * 32 if change == "wrong-claim" else claim,
        source=source,
    )
    assert proof.state.value == expected
    if expected == "CHANGED":
        assert proof.reason
        assert proof.release is None
    elif expected == "RELEASED_CLEAN":
        assert proof.release["released_owner"] == owner
        assert proof.before_release().fold(card).model_dump() == original


def test_admission_held_status_policy_is_explicit(tmp_path):
    store, card, owner, _, _ = prepared(tmp_path)
    claim = store.fold(card).meta["_claim_revision"]
    assert inspect_custody(tmp_path, card, owner, claim).state == CustodyState.HELD
    assert (
        inspect_custody(tmp_path, card, owner, claim, held_statuses=frozenset()).state
        == CustodyState.CHANGED
    )


def test_snapshot_reads_each_ledger_once(tmp_path, monkeypatch):
    from skcoord.card_store import CardStore

    store, card, owner, _, _ = prepared(tmp_path)
    claim = store.fold(card).meta["_claim_revision"]
    counts = {}
    for name in ("_read_events", "_legacy_events"):
        original = getattr(CardStore, name)

        def counted(self, card_id, original=original, name=name):
            counts[name] = counts.get(name, 0) + 1
            return original(self, card_id)

        monkeypatch.setattr(CardStore, name, counted)
    proof = inspect_custody(tmp_path, card, owner, claim)
    proof.store.fold(card)
    assert counts == {"_read_events": 1, "_legacy_events": 1}
