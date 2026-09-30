"""Guarded receipt writes survive lost replies without duplicate applicability."""

import hashlib
import json

import pytest

from skcapstone.card import CardEventLog
from skcapstone.card_store import CardStore
from skcapstone.review_verdict import _card_events, _source_only_applicability
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_guarded_completion import prepared
from tests.test_guarded_review_work import prepared as prepared_review
from tests.test_provisional_verdict_producer import _run


def arguments(home, card, owner, claim, key, value):
    """Pin the native pre-write revision, not a version derived after mutation."""
    return [
        "link",
        card,
        key,
        value,
        "--agent",
        owner,
        "--expected-source-revision",
        LiveCardStoreGateway(home).read_card(card).revision,
        "--expected-claim-revision",
        claim,
        "--transition-id",
        hashlib.sha256((card + key + value).encode()).hexdigest(),
    ]


def test_native_and_overlay_each_record_one_identical_event(tmp_path):
    store, card, owner, _ = prepared(tmp_path)
    args = arguments(tmp_path, card, owner, "c" * 32, "verdict", "PASS")
    for _ in range(2):
        result = _run(tmp_path, *args)
        assert result.exit_code == 0, result.output
    native = [e for e in store._read_events(card) if e["action"] == "link"]
    overlay = list(_card_events(card, tmp_path))
    assert len(native) == len(overlay) == 1
    assert native[0]["event_id"] == overlay[0]["event_id"]
    assert native[0]["ts"] == overlay[0]["ts"]
    assert store.fold(card).links["verdict"] == "PASS"


def test_guarded_json_acknowledgement_names_exact_current_revision(tmp_path):
    store, card, owner, _ = prepared(tmp_path)
    args = arguments(tmp_path, card, owner, "c" * 32, "verdict", "PASS") + ["--json"]
    first = _run(tmp_path, *args)
    assert first.exit_code == 0, first.output
    readback = json.loads(first.output)
    assert readback["card_id"] == card
    assert readback["source_revision"] == LiveCardStoreGateway(tmp_path).read_card(card).revision
    second = _run(tmp_path, *args)
    assert json.loads(second.output) == readback


@pytest.mark.parametrize("point", ["native", "overlay"])
def test_lost_reply_recovers_same_event_and_projection(tmp_path, monkeypatch, point):
    store, card, owner, _ = prepared(tmp_path)
    args = arguments(tmp_path, card, owner, "c" * 32, "evidence", "exact fixture")
    if point == "native":
        original = CardStore.append_event

        def interrupted(self, *values, **kwargs):
            original(self, *values, **kwargs)
            raise ValueError("lost native acknowledgement")

        target, method = CardStore, "append_event"
    else:
        original = CardEventLog.append

        def interrupted(self, *values, **kwargs):
            original(self, *values, **kwargs)
            raise ValueError("lost overlay acknowledgement")

        target, method = CardEventLog, "append"
    monkeypatch.setattr(target, method, interrupted)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    monkeypatch.setattr(target, method, original)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert len([e for e in store._read_events(card) if e["action"] == "link"]) == 1
    assert len(list(_card_events(card, tmp_path))) == 1


@pytest.mark.parametrize("change", ["claim", "contract", "outcome", "link", "payload"])
@pytest.mark.parametrize("after_first", [False, True])
def test_stale_link_request_never_adds_or_replaces_evidence(tmp_path, change, after_first):
    store, card, owner, _ = prepared(tmp_path)
    args = arguments(tmp_path, card, owner, "c" * 32, "evidence", "exact fixture")
    if after_first:
        result = _run(tmp_path, *args)
        assert result.exit_code == 0, result.output
    if change == "claim":
        store.append_event(card, "claim", owner, owner=owner, claim_revision="d" * 32)
    elif change == "contract":
        store.append_event(card, "describe", owner, description="changed")
    elif change == "outcome":
        store.append_event(card, "verdict", owner, verdict="FAIL")
    elif change == "link":
        store.append_event(card, "link", owner, link_key="other", link_value="changed")
    else:
        # A different request cannot reuse the same deterministic transition.
        if not after_first:
            args[-1] = "malformed transition"
        else:
            args[3] = "changed fixture"
    native, overlay = store._read_events(card), list(_card_events(card, tmp_path))
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert store._read_events(card) == native
    assert list(_card_events(card, tmp_path)) == overlay


def test_guarded_receipt_order_satisfies_existing_native_review_gate(tmp_path):
    store, source, producer, artifact, args = prepared_review(tmp_path)
    opened = _run(tmp_path, *args)
    assert opened.exit_code == 0, opened.output
    review = json.loads(opened.output)["review_card_id"]
    owner, claim = "pi-seraph-fiber-" + review, "d" * 32
    store.append_event(review, "claim", owner, owner=owner, claim_revision=claim)
    report = tmp_path / "review.md"
    report.write_text("Synthetic independent review fixture, not live acceptance.\n")
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    for key, value in [("verdict", "PASS"), ("review_evidence", f"{report}|sha256={digest}")]:
        result = _run(tmp_path, *arguments(tmp_path, review, owner, claim, key, value))
        assert result.exit_code == 0, result.output
    assert not _source_only_applicability(review, tmp_path)
    receipt = json.dumps(
        {
            "type": "source-only-applicability",
            "card_id": review,
            "source_head": store.fold(review).meta["link_head_revision"],
            "reviewer": owner,
            "evidence_digest": digest,
            "governed_pr_ci": False,
        },
        sort_keys=True,
    )
    last = arguments(tmp_path, review, owner, claim, "applicability_receipt", receipt)
    for _ in range(2):
        result = _run(tmp_path, *last)
        assert result.exit_code == 0, result.output
    assert _source_only_applicability(review, tmp_path)
    completed = _run(
        tmp_path,
        "complete",
        review,
        "--agent",
        owner,
        "--expected-source-revision",
        LiveCardStoreGateway(tmp_path).read_card(review).revision,
        "--expected-claim-revision",
        claim,
    )
    assert completed.exit_code == 0, completed.output
    assert store.fold(review).status.value == "done"
    assert store.fold(source).owner == producer
    assert (
        len(
            [
                e
                for e in _card_events(review, tmp_path)
                if e.get("link_key") == "applicability_receipt"
            ]
        )
        == 1
    )


def test_native_outcome_change_during_projection_is_not_acknowledged(tmp_path, monkeypatch):
    """The acknowledgement must not adopt an unrelated racing typed outcome."""
    store, card, owner, _ = prepared(tmp_path)
    args = arguments(tmp_path, card, owner, "c" * 32, "evidence", "exact fixture")
    original = CardEventLog.append

    def raced(self, event):
        store.append_event(card, "verdict", owner, verdict="FAIL")
        return original(self, event)

    monkeypatch.setattr(CardEventLog, "append", raced)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
