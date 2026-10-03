"""Closing the producer also locks the exact current completed review."""

import hashlib
import json

import pytest

from skcapstone.coordination import Board
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.guarded_producer_fixtures import prepared_review
from tests.test_provisional_verdict_producer import _run


def accepted_review(home):
    """Create a synthetic native PASS review; never represent live acceptance."""
    store, source, producer, artifact, review = prepared_review(home)
    reviewer = "pi-seraph-fiber-" + review
    store.append_event(review, "claim", reviewer, owner=reviewer, claim_revision="d" * 32)
    report = home / "synthetic-review.md"
    report.write_text("Synthetic fixture only.\n")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    source_head = store.fold(review).meta["link_head_revision"]

    def link(card, owner, key, value):
        result = _run(home, "link", card, key, value, "--agent", owner)
        assert result.exit_code == 0, result.output

    link(review, reviewer, "verdict", "PASS")
    link(review, reviewer, "review_evidence", f"{report}|sha256={digest}")
    link(
        review,
        reviewer,
        "applicability_receipt",
        json.dumps(
            {
                "type": "source-only-applicability",
                "card_id": review,
                "source_head": source_head,
                "reviewer": reviewer,
                "evidence_digest": digest,
                "governed_pr_ci": False,
            }
        ),
    )
    completed = _run(home, "complete", review, "--agent", reviewer)
    assert completed.exit_code == 0, completed.output
    link(source, producer, "head", source_head)
    link(
        source,
        producer,
        "candidate_evidence_sha256",
        hashlib.sha256(artifact.read_bytes()).hexdigest(),
    )
    link(source, producer, "verdict", "PASS")
    gateway = LiveCardStoreGateway(home)
    args = [
        "complete",
        source,
        "--agent",
        producer,
        "--expected-source-revision",
        gateway.read_card(source).revision,
        "--expected-claim-revision",
        "c" * 32,
        "--review-card",
        review,
        "--expected-review-revision",
        gateway.read_card(review).revision,
    ]
    return store, source, review, producer, reviewer, args


def test_source_completion_uses_the_exact_completed_review(tmp_path):
    store, source, review, producer, reviewer, args = accepted_review(tmp_path)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert store.fold(source).status.value == "done"
    assert store.fold(source).owner is None
    assert store.fold(review).status.value == "done"


@pytest.mark.parametrize("change", ["verdict", "receipt", "head", "reopen"])
def test_changed_review_cannot_close_the_producer(tmp_path, change):
    store, source, review, producer, reviewer, args = accepted_review(tmp_path)
    if change == "reopen":
        reopened = _run(
            tmp_path, "reopen", review, "--reason", "synthetic recheck", "--agent", "jarvis"
        )
        assert reopened.exit_code == 0, reopened.output
        assert store.fold(review).status.value != "done"
    else:
        key, value = {
            "verdict": ("verdict", "FAIL"),
            "head": ("head", "e" * 40),
            "receipt": ("review_evidence", "changed"),
        }[change]
        store.append_event(review, "link", reviewer, link_key=key, link_value=value)
    before = store._read_events(source)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert store._read_events(source) == before
    assert store.fold(source).owner == producer


def test_review_race_inside_native_completion_is_refused(tmp_path, monkeypatch):
    store, source, review, producer, reviewer, args = accepted_review(tmp_path)
    original = Board.complete_task

    def raced(self, agent, card, **kwargs):
        store.append_event(review, "link", reviewer, link_key="verdict", link_value="FAIL")
        return original(self, agent, card, **kwargs)

    monkeypatch.setattr(Board, "complete_task", raced)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert not [e for e in store._read_events(source) if e["action"] == "complete"]


def test_review_guards_cannot_silently_use_legacy_completion(tmp_path):
    store, source, review, producer, reviewer, args = accepted_review(tmp_path)
    result = _run(tmp_path, "complete", source, "--agent", producer, "--review-card", review)
    assert result.exit_code != 0
    assert store.fold(source).owner == producer
