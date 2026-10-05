"""The dispatcher may open exactly one review, never complete or launch it."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.provisional_verdict import candidate_evidence
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_provisional_verdict_producer import COMMIT, REF, TREE, _candidate, _run


def prepared(home: Path):
    """Seed only an isolated, typed, source-only producer candidate."""
    card, owner, claim = "1234abcd", "pi-fiber-implementation-1234abcd", "c" * 32
    store = CardStore(home)
    store.create(
        CardCore(
            id=card,
            title="[S] Exact source task",
            description="Preserve the original contract.",
            created_by=owner,
            initial_labels=["source-only", "sklegal"],
            acceptance_criteria=["Run the required tests and independently verify their results."],
            meta={
                "repository": "https://github.com/org/repo.git",
                "base_ref": "main",
                "base_revision": "e" * 40,
            },
        )
    )
    store.append_event(card, "claim", owner, owner=owner, claim_revision=claim)
    artifact = _candidate(home, card)
    store.append_event(
        card,
        "verdict",
        owner,
        verdict="PASS_FOR_REVIEW",
        **candidate_evidence(artifact, COMMIT, TREE, REF),
    )
    revision = LiveCardStoreGateway(home).read_card(card).revision
    args = [
        "review-work",
        card,
        "--producer",
        owner,
        "--agent",
        "link",
        "--expected-source-revision",
        revision,
        "--expected-claim-revision",
        claim,
    ]
    return store, card, owner, artifact, args


def test_cli_opens_one_native_review_without_claim_or_completion(tmp_path):
    store, card, owner, artifact, args = prepared(tmp_path)
    before = store._read_events(card)
    first = _run(tmp_path, *args)
    assert first.exit_code == 0, first.output
    result = json.loads(first.output)
    second = _run(tmp_path, *args)
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["created"] is False
    review = store.fold(result["review_card_id"])
    assert result["launchable"] is True
    assert review.owner is None
    assert review.status.value == "review"
    assert store._read_events(card) == before
    assert review.meta["producer_identity"] == owner
    assert (
        review.meta["candidate_evidence_sha256"]
        == hashlib.sha256(artifact.read_bytes()).hexdigest()
    )
    assert review.meta["link_head_revision"] == COMMIT
    assert review.meta["source_revision"] == args[args.index("--expected-source-revision") + 1]
    for text in [str(artifact), COMMIT, TREE, REF, "Preserve the original contract."]:
        assert text in review.description
    for text in (
        "must create a local review-evidence commit",
        f"docs/evidence/agents/{review.id}/",
        "git status --porcelain",
        "must be clean",
        f"Keep source_head equal to the reviewed candidate {COMMIT}",
        "do not modify source or tests",
        "no push",
    ):
        assert text in review.description
    assert any("required tests" in item for item in review.acceptance_criteria)
    assert len(store.list_card_ids()) == 2
    events = store._read_events(review.id)
    assert sum(event["action"] == "review_assignment_recommendation" for event in events) == 1


def test_non_link_actor_cannot_gain_review_assignment_authority(tmp_path):
    store, card, owner, artifact, args = prepared(tmp_path)
    args[args.index("--agent") + 1] = owner
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "Invalid value for '--agent'" in result.output
    assert store.list_card_ids() == [card]


@pytest.mark.parametrize("receipt", ["chiap03:native-receipt-1", "chiap03:native-receipt-2"])
def test_supplemental_native_qualification_preserves_retained_review(tmp_path, receipt):
    """Operator test evidence does not replace a typed source generation."""
    store, card, owner, artifact, args = prepared(tmp_path)
    opened = _run(tmp_path, *args)
    assert opened.exit_code == 0, opened.output
    review_id = json.loads(opened.output)["review_card_id"]
    before = LiveCardStoreGateway(tmp_path).read_card(card).revision
    store.append_event(
        card,
        "link",
        "jarvis",
        link_key="native_semantic_candidate_qualification",
        link_value=receipt,
    )
    assert LiveCardStoreGateway(tmp_path).read_card(card).revision == before
    retried = _run(tmp_path, *args)
    assert retried.exit_code == 0, retried.output
    assert json.loads(retried.output)["review_card_id"] == review_id
    assert json.loads(retried.output)["created"] is False
    assert store.fold(review_id).meta["source_revision"] == before
    assert store.fold(card).owner == owner
    assert store.fold(card).links["native_semantic_candidate_qualification"] == receipt


@pytest.mark.parametrize("change", ["criteria", "dependency", "repository", "other-link"])
def test_supplemental_qualification_does_not_hide_source_changes(tmp_path, change):
    """The exemption never covers instructions or arbitrary metadata."""
    store, card, owner, artifact, args = prepared(tmp_path)
    store.append_event(
        card,
        "link",
        "jarvis",
        link_key="native_semantic_candidate_qualification",
        link_value="chiap03:native-receipt",
    )
    before = LiveCardStoreGateway(tmp_path).read_card(card).revision
    if change == "criteria":
        store.append_event(card, "amend_criteria", owner, criteria=["Changed contract."])
    elif change == "dependency":
        store.append_event(card, "add_dependency", owner, dependency="deadbeef")
    else:
        key = "repository" if change == "repository" else "new_source_input"
        store.append_event(card, "link", owner, link_key=key, link_value="https://other/repo")
    if change == "repository":
        from skcapstone.seraph_review_contracts import ReviewPublicationError

        with pytest.raises(ReviewPublicationError, match="card_binding_conflict"):
            LiveCardStoreGateway(tmp_path).read_card(card)
        return
    assert LiveCardStoreGateway(tmp_path).read_card(card).revision != before


def test_review_card_qualification_link_still_changes_review_revision(tmp_path):
    """Supplemental producer evidence cannot hide changes on a review itself."""
    store, card, owner, artifact, args = prepared(tmp_path)
    opened = _run(tmp_path, *args)
    assert opened.exit_code == 0, opened.output
    review_id = json.loads(opened.output)["review_card_id"]
    before = LiveCardStoreGateway(tmp_path).read_card(review_id).revision
    store.append_event(
        review_id,
        "link",
        "jarvis",
        link_key="native_semantic_candidate_qualification",
        link_value="chiap03:native-receipt",
    )
    assert LiveCardStoreGateway(tmp_path).read_card(review_id).revision != before


@pytest.mark.parametrize(
    "change", ["claim", "owner", "contract", "outcome", "artifact", "done", "label"]
)
def test_changed_inputs_create_nothing(tmp_path, change):
    store, card, owner, artifact, args = prepared(tmp_path)
    if change == "claim":
        store.append_event(card, "claim", owner, owner=owner, claim_revision="d" * 32)
    elif change == "owner":
        store.append_event(card, "unassign", owner)
    elif change == "contract":
        store.append_event(card, "describe", owner, description="changed")
    elif change == "outcome":
        store.append_event(card, "verdict", owner, verdict="FAIL")
    elif change == "artifact":
        artifact.write_text("different bytes")
    elif change == "label":
        store.append_event(card, "remove_label", owner, label="source-only")
    else:
        store.append_event(card, "complete", owner)
    before = store._read_events(card)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "guarded review" in result.output
    assert store.list_card_ids() == [card]
    assert store._read_events(card) == before


@pytest.mark.parametrize("change", ["outside", "symlink", "empty", "large", "malformed"])
def test_invalid_candidate_is_refused_before_creation(tmp_path, change):
    store, card, owner, artifact, args = prepared(tmp_path)
    if change == "outside":
        artifact = tmp_path / "elsewhere.patch"
        artifact.write_text("outside")
    elif change == "symlink":
        target = artifact.with_name("target.patch")
        target.write_bytes(artifact.read_bytes())
        artifact.unlink()
        artifact.symlink_to(target)
    elif change == "empty":
        artifact.write_bytes(b"")
    elif change == "large":
        artifact.write_bytes(b"x" * (262144 + 1))
    payload = candidate_evidence(artifact, COMMIT, TREE, REF)
    if change == "symlink":
        payload["candidate_path"] = str(artifact)
    if change == "malformed":
        payload["candidate_tree"] = "not-a-tree"
    store.append_event(card, "verdict", owner, verdict="PASS_FOR_REVIEW", **payload)
    args[args.index("--expected-source-revision") + 1] = (
        LiveCardStoreGateway(tmp_path).read_card(card).revision
    )
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "guarded review candidate" in result.output
    assert store.list_card_ids() == [card]


def test_replaced_typed_generation_does_not_reuse_old_review(tmp_path):
    store, card, owner, artifact, args = prepared(tmp_path)
    assert _run(tmp_path, *args).exit_code == 0
    store.append_event(
        card,
        "verdict",
        owner,
        verdict="PASS_FOR_REVIEW",
        **candidate_evidence(artifact, COMMIT, TREE, REF),
    )
    args[args.index("--expected-source-revision") + 1] = (
        LiveCardStoreGateway(tmp_path).read_card(card).revision
    )
    before = {cid: store._read_events(cid) for cid in store.list_card_ids()}
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert {cid: store._read_events(cid) for cid in store.list_card_ids()} == before


def test_concurrent_open_is_one_review(tmp_path):
    from skcapstone.guarded_review_work import open_guarded_review

    store, card, owner, artifact, args = prepared(tmp_path)
    revision = args[args.index("--expected-source-revision") + 1]

    def run(_):
        return open_guarded_review(
            tmp_path,
            card,
            owner,
            expected_source_revision=revision,
            expected_claim_revision="c" * 32,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert sum(result.created for result in results) == 1
    assert len(store.list_card_ids()) == 2


@pytest.mark.parametrize("verdict", ["FAIL", "PASS_FOR_REVIEW"])
def test_racing_typed_outcome_refuses_dispatch_after_reconciliation(
    tmp_path, monkeypatch, verdict
):
    """The source snapshot hashes outcomes, not only folded card fields."""
    from skcapstone import guarded_review_work

    store, card, owner, artifact, args = prepared(tmp_path)
    original = guarded_review_work.reconcile_review_work

    def raced(*call_args, **kwargs):
        result = original(*call_args, **kwargs)
        store.append_event(
            card,
            "verdict",
            owner,
            verdict=verdict,
            **candidate_evidence(artifact, COMMIT, TREE, REF),
        )
        return result

    monkeypatch.setattr(guarded_review_work, "reconcile_review_work", raced)
    result = _run(tmp_path, *args)
    assert result.exit_code != 0
    assert "source changed during reconciliation" in result.output
    assert len(store.list_card_ids()) == 2  # Preserve the audit, but never dispatch it.
    review = next(row for row in store.list_cards() if row.id != card)
    assert review.owner is None
    assert not any(event["action"] == "complete" for event in store._read_events(card))


def test_retry_after_create_acknowledgement_loss_finishes_same_review(tmp_path, monkeypatch):
    store, card, owner, artifact, args = prepared(tmp_path)
    original = CardStore.create

    def lost_ack(self, core):
        result = original(self, core)
        if core.id != card:
            raise ValueError("synthetic lost create acknowledgement")
        return result

    monkeypatch.setattr(CardStore, "create", lost_ack)
    first = _run(tmp_path, *args)
    assert first.exit_code != 0
    assert len(store.list_card_ids()) == 2
    monkeypatch.setattr(CardStore, "create", original)
    retried = _run(tmp_path, *args)
    assert retried.exit_code == 0, retried.output
    review = store.fold(json.loads(retried.output)["review_card_id"])
    assert review.status.value == "review"
    assert review.links["producer_identity"] == owner
    assert review.owner is None
    assert len(store.list_card_ids()) == 2


def test_source_only_receipt_recognizes_new_review_metadata(tmp_path):
    from skcapstone.review_verdict import _source_only_applicability

    store, card, owner, artifact, args = prepared(tmp_path)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    review_id = json.loads(result.output)["review_card_id"]
    reviewer = "pi-seraph-fiber-" + review_id
    store.append_event(review_id, "claim", reviewer, owner=reviewer, claim_revision="f" * 32)
    evidence = tmp_path / "review.md"
    evidence.write_text("Independent source-only verification, not hosted CI.")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()

    def link(key, value):
        result = _run(tmp_path, "link", review_id, key, value, "--agent", reviewer)
        assert result.exit_code == 0, result.output

    link("verdict", "PASS")
    link("review_evidence", f"{evidence}|sha256={digest}")
    assert not _source_only_applicability(review_id, tmp_path)
    receipt = {
        "type": "source-only-applicability",
        "card_id": review_id,
        "source_head": COMMIT,
        "reviewer": reviewer,
        "evidence_digest": digest,
        "governed_pr_ci": False,
    }
    link("applicability_receipt", json.dumps(receipt, sort_keys=True))
    assert _source_only_applicability(review_id, tmp_path)
    link("review_evidence", f"{evidence}|sha256={digest}")
    assert not _source_only_applicability(review_id, tmp_path)


def test_native_cli_review_completion_needs_receipt_and_leaves_source_owned(tmp_path):
    store, card, owner, artifact, args = prepared(tmp_path)
    opened = _run(tmp_path, *args)
    assert opened.exit_code == 0, opened.output
    review_id = json.loads(opened.output)["review_card_id"]
    reviewer = "pi-seraph-fiber-" + review_id
    # Seed the isolated completion fixture; do not consult live fleet capacity.
    store.append_event(review_id, "claim", reviewer, owner=reviewer, claim_revision="f" * 32)
    evidence = tmp_path / "review-completion.md"
    evidence.write_text("Synthetic independent review fixture. Not live acceptance or CI.")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    for key, value in [("verdict", "PASS"), ("review_evidence", f"{evidence}|sha256={digest}")]:
        result = _run(tmp_path, "link", review_id, key, value, "--agent", reviewer)
        assert result.exit_code == 0, result.output
    missing = _run(tmp_path, "complete", review_id, "--agent", reviewer)
    assert missing.exit_code != 0
    assert store.fold(review_id).status.value != "done"
    receipt = {
        "type": "source-only-applicability",
        "card_id": review_id,
        "source_head": COMMIT,
        "reviewer": reviewer,
        "evidence_digest": digest,
        "governed_pr_ci": False,
    }
    linked = _run(
        tmp_path,
        "link",
        review_id,
        "applicability_receipt",
        json.dumps(receipt, sort_keys=True),
        "--agent",
        reviewer,
    )
    assert linked.exit_code == 0, linked.output
    completed = _run(tmp_path, "complete", review_id, "--agent", reviewer)
    assert completed.exit_code == 0, completed.output
    assert store.fold(review_id).status.value == "done"
    assert store.fold(review_id).owner is None
    assert store.fold(card).owner == owner
    assert store.fold(card).status.value != "done"
