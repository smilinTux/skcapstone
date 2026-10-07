"""Late native readback links must not strand an unopened source review."""

from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.review_dispatch import _revision_drift_is_supplemental
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway


def _open_review(home: Path):
    store = CardStore(home)
    store.create(CardCore(id="source01", title="Source"))
    pinned = LiveCardStoreGateway(home).read_card("source01").revision
    review_id = "e41dd12d"
    digest = "a" * 64
    store.create(
        CardCore(
            id=review_id,
            title="[S][REVIEW] Exact source review",
            initial_labels=["review", "source-only", "seat-seraph", "parent-source01"],
            meta={"source_revision": pinned, "candidate_evidence_sha256": digest},
        )
    )
    store.append_event(review_id, "move", "link", column="review")
    store.append_event(
        review_id,
        "review_assignment_recommendation",
        "link",
        transition_id="link-review-" + review_id,
        recommendation_id="link-review-" + review_id,
        evidence_sha256=digest,
        reviewer="pi-seraph-fiber-" + review_id,
        author="builder",
        observed_state_revision="b" * 64,
        observed_process={"sessions": []},
    )
    source = store.fold("source01")
    review = store.fold(review_id)
    return store, source, review, pinned


def test_allows_only_late_native_readback_link_before_review_launch(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store, source, review, pinned = _open_review(home)
    store.append_event(
        source.id,
        "link",
        "jarvis",
        link_key="native_glm_readback",
        link_value="synthetic readback sha256=" + "c" * 64,
    )
    current = LiveCardStoreGateway(home).read_card(source.id).revision

    assert current != pinned
    assert _revision_drift_is_supplemental(home, review, source, pinned, current)


def test_rejects_other_source_changes_and_started_reviews(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    store, source, review, pinned = _open_review(home)
    store.append_event(source.id, "add_label", "jarvis", label="new-contract")
    changed = LiveCardStoreGateway(home).read_card(source.id).revision
    assert not _revision_drift_is_supplemental(home, review, source, pinned, changed)

    home2 = tmp_path / "claimed"
    home2.mkdir()
    store2, source2, review2, pinned2 = _open_review(home2)
    store2.append_event(
        source2.id,
        "link",
        "jarvis",
        link_key="native_glm_readback",
        link_value="synthetic readback sha256=" + "d" * 64,
    )
    store2.append_event(
        review2.id,
        "claim",
        "pi-seraph-fiber-e41dd12d",
        owner="pi-seraph-fiber-e41dd12d",
        claim_revision="e" * 32,
    )
    current2 = LiveCardStoreGateway(home2).read_card(source2.id).revision
    assert not _revision_drift_is_supplemental(home2, review2, source2, pinned2, current2)
