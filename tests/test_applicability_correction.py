"""A correction never impersonates a reviewer or changes the review verdict."""

import copy
import json
from types import SimpleNamespace

import pytest

from skcapstone import applicability_correction as correction


@pytest.fixture
def proof(monkeypatch):
    receipt = {
        "type": "source-only-applicability",
        "card_id": "123abcde",
        "source_head": "b" * 40,
        "reviewer": "reviewer",
        "evidence_digest": "e" * 64,
        "governed_pr_ci": False,
    }
    row = SimpleNamespace(
        archived=False,
        owner="reviewer",
        meta={
            "_claim_revision": "claim",
            "link_source_card": "87654321",
            "candidate_tree": "c" * 40,
        },
    )
    decision = {
        "schema": "skfleet.source-review-decision/v1",
        "card": "123abcde",
        "parent_card": "87654321",
        "reviewer_identity": "reviewer",
        "source_head": "a" * 40,
        "source_tree": "c" * 40,
        "verdict": "PASS",
        "report_sha256": "e" * 64,
    }
    value = {
        "schema": "skfleet.applicability-head-correction/v1",
        "card": "123abcde",
        "claim_revision": "claim",
        "receipt_sha256": correction.receipt_digest(receipt),
        "source_head": "a" * 40,
        "review_head": "b" * 40,
        "decision": decision,
        "reason": "Verified decision and two-file diff: receipt names evidence commit",
    }
    monkeypatch.setattr(
        correction, "CardStore", lambda home: SimpleNamespace(fold=lambda card: row)
    )
    return receipt, value, row


def event(value, writer="lumina-nor"):
    return {"link_key": correction.KEY, "link_value": json.dumps(value), "writer": writer}


def test_exact_operator_correction_preserves_original(proof, tmp_path):
    receipt, value, _ = proof
    before = copy.deepcopy(receipt)
    result = correction.corrected_receipt(tmp_path, "123abcde", receipt, "a" * 40, [event(value)])
    assert result == dict(receipt, source_head="a" * 40)
    assert receipt == before


@pytest.mark.parametrize(
    "change", ["actor", "claim", "digest", "decision", "owner", "head", "conflict"]
)
def test_changed_correction_cannot_authorize_receipt(proof, tmp_path, change):
    receipt, value, row = proof
    writer = "lumina-nor"
    if change == "actor":
        writer = "foreign"
    if change == "claim":
        value["claim_revision"] = "other"
    if change == "digest":
        value["receipt_sha256"] = "f" * 64
    if change == "decision":
        value["decision"]["source_head"] = "d" * 40
    if change == "owner":
        row.owner = "other"
    if change == "head":
        value["source_head"] = "d" * 40
    events = [event(value, writer)]
    if change == "conflict":
        events.append(event(dict(value, reason="another correction")))
    if change == "claim":
        assert (
            correction.corrected_receipt(tmp_path, "123abcde", receipt, "a" * 40, events)
            == receipt
        )
    else:
        with pytest.raises(ValueError):
            correction.corrected_receipt(tmp_path, "123abcde", receipt, "a" * 40, events)
