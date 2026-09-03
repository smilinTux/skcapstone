"""Tests for bounded, revision-fenced Link PR triage."""

from dataclasses import replace

import pytest

from skcapstone.link_cycle import (
    CycleState,
    ProducerIdentity,
    PullRequestObservation,
    ReviewerIdentity,
    TerminalReviewEvidence,
    join_review_evidence,
    next_batch,
    recommend_one_reviewer,
    validate_handoff,
)
from skcapstone.seat_boundaries import BoundaryError

HEAD = "a" * 40
BASE = "b" * 40
HASH = "c" * 64
GENERATION = "generation-7"


def pr(number: int = 1, **changes) -> PullRequestObservation:
    values = {
        "repository": "smilinTux/example",
        "number": number,
        "title": "Normal source repair",
        "author": "producer",
        "head_sha": HEAD,
        "base_sha": BASE,
        "ci_state": "success",
        "conflict_state": "clean",
        "review_requests": (),
        "source_card": "deadbeef",
        "card_generation": GENERATION,
        "observed_at": "2026-09-02T12:00:00Z",
    }
    values.update(changes)
    return PullRequestObservation(**values)


def producer() -> ProducerIdentity:
    return ProducerIdentity("producer-key", "chiap01", "source-session", "/work/source")


def reviewers() -> list[ReviewerIdentity]:
    return [
        ReviewerIdentity("same-host", "other-key", "chiap01", "review-1", "/work/r1"),
        ReviewerIdentity("same-session", "other-key", "chiap02", "source-session", "/work/r2"),
        ReviewerIdentity("same-workspace", "other-key", "chiap02", "review-2", "/work/source"),
        ReviewerIdentity("eligible", "review-key", "chiap02", "review-3", "/work/r3"),
        ReviewerIdentity("extra", "review-key-2", "chiap03", "review-4", "/work/r4"),
    ]


def handoff(observation: PullRequestObservation | None = None):
    return recommend_one_reviewer(
        observation or pr(),
        producer=producer(),
        candidates=reviewers(),
        review_card_id="feedface",
        review_card_revision=HASH,
    )


def evidence(**changes) -> TerminalReviewEvidence:
    values = {
        "review_card_id": "feedface",
        "source_card": "deadbeef",
        "card_generation": GENERATION,
        "head_sha": HEAD,
        "reviewer": "eligible",
        "verdict": "PASS",
        "evidence_sha256": HASH,
    }
    values.update(changes)
    return TerminalReviewEvidence(**values)


def test_inventory_revision_covers_all_required_fields() -> None:
    item = pr()
    assert item.classification == "needs-reviewer"
    assert len(item.observation_revision) == 64
    assert replace(item, base_sha="d" * 40).observation_revision != item.observation_revision
    assert (
        replace(item, review_requests=("reviewer",)).observation_revision
        != item.observation_revision
    )
    assert (
        replace(item, card_generation="generation-8").observation_revision
        != item.observation_revision
    )


@pytest.mark.parametrize(
    ("changes", "classification"),
    [
        ({"ci_state": "failure"}, "needs-work"),
        ({"conflict_state": "conflicting"}, "needs-work"),
        ({"ci_state": "pending"}, "stale"),
        ({"source_card": None, "card_generation": None}, "orphaned"),
        ({"review_requests": ("reviewer",)}, "review-active"),
        ({"active_review_card": "feedface"}, "review-active"),
    ],
)
def test_inventory_classification(changes: dict, classification: str) -> None:
    assert pr(**changes).classification == classification


def test_exactly_one_distinct_reviewer_is_recommended() -> None:
    item = handoff()
    assert item is not None
    assert item.reviewer == "eligible"
    assert item.head_sha == HEAD
    assert item.card_generation == GENERATION
    assert item.as_event()["schema"] == "skfleet.pr-review-handoff/v1"


def test_existing_request_or_active_review_suppresses_duplicate() -> None:
    assert handoff(pr(review_requests=("someone",))) is None
    assert handoff(pr(active_review_card="feedface")) is None


def test_no_distinct_reviewer_fails_closed() -> None:
    with pytest.raises(BoundaryError, match="no distinct"):
        recommend_one_reviewer(
            pr(),
            producer=producer(),
            candidates=reviewers()[:3],
            review_card_id="feedface",
            review_card_revision=HASH,
        )


def test_jarvis_validates_fresh_handoff() -> None:
    item = handoff()
    assert item is not None
    validate_handoff(
        item,
        current_observation=pr(),
        current_review_card_revision=HASH,
        active_review_card=None,
        used_recommendation_ids=set(),
        actor="jarvis",
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"current_observation": pr(head_sha="d" * 40)}, "revision changed"),
        ({"current_observation": pr(card_generation="generation-8")}, "revision changed"),
        ({"current_review_card_revision": "d" * 64}, "review card revision changed"),
        ({"active_review_card": "other-card"}, "active review"),
        ({"used_recommendation_ids": None}, "replay"),
        ({"actor": "link"}, "only jarvis"),
    ],
)
def test_stale_duplicate_and_non_jarvis_handoffs_fail_closed(kwargs: dict, message: str) -> None:
    item = handoff()
    assert item is not None
    arguments = {
        "current_observation": pr(),
        "current_review_card_revision": HASH,
        "active_review_card": None,
        "used_recommendation_ids": set(),
        "actor": "jarvis",
    }
    if kwargs.get("used_recommendation_ids") is None and "used_recommendation_ids" in kwargs:
        kwargs["used_recommendation_ids"] = {item.recommendation_id}
    arguments.update(kwargs)
    with pytest.raises(BoundaryError, match=message):
        validate_handoff(item, **arguments)


def test_terminal_pass_joins_to_exact_head_and_generation() -> None:
    result = join_review_evidence(pr(), evidence())
    assert result.classification == "merge-eligible"
    assert result.eligible
    assert result.authority == "recommendation-only"
    assert len(result.evidence_sha256) == 64


@pytest.mark.parametrize(
    ("review", "failure"),
    [
        (None, "missing-terminal-review-evidence"),
        (evidence(head_sha="d" * 40), "review-head-mismatch"),
        (evidence(card_generation="generation-8"), "review-card-generation-mismatch"),
        (evidence(source_card="cafebabe"), "review-source-card-mismatch"),
        (evidence(verdict="BLOCKED"), "missing-independent-pass"),
    ],
)
def test_merge_eligibility_requires_explicit_exact_evidence(review, failure: str) -> None:
    result = join_review_evidence(pr(), review)
    assert not result.eligible
    assert failure in result.failures


def test_lifecycle_or_links_alone_never_imply_approval() -> None:
    observation = pr(active_review_card="feedface", lineage_outcomes=("done", "review-linked"))
    result = join_review_evidence(observation, None)
    assert not result.eligible
    assert "missing-terminal-review-evidence" in result.failures


def test_cycle_is_bounded_and_wraps_without_starvation() -> None:
    inventory = [pr(number=index) for index in range(1, 6)]
    first = next_batch(inventory, CycleState(), limit=2, revisit_after_cycles=2)
    second = next_batch(inventory, first.state, limit=2, revisit_after_cycles=2)
    third = next_batch(inventory, second.state, limit=2, revisit_after_cycles=2)
    assert [[item.number for item in batch.observations] for batch in (first, second, third)] == [
        [1, 2],
        [3, 4],
        [5, 1],
    ]


def test_changed_and_retry_items_are_revisited_without_duplicate_active_review() -> None:
    item = pr()
    first = next_batch([item], CycleState(), limit=1)
    unchanged = next_batch([item], first.state, limit=1)
    assert unchanged.observations == ()
    changed = replace(item, head_sha="d" * 40)
    selected = next_batch([changed], unchanged.state, limit=1, retry_keys={changed.key})
    assert selected.observations == (changed,)
    assert selected.state.entries[changed.key].attempts == 1
    assert handoff(replace(changed, active_review_card="feedface")) is None


def test_retry_policy_caps_attempts_and_uses_bounded_backoff() -> None:
    item = pr()
    state = CycleState()
    for _ in range(8):
        batch = next_batch(
            [item], state, limit=1, retry_keys={item.key}, max_retries=3, revisit_after_cycles=2
        )
        state = batch.state
    entry = state.entries[item.key]
    assert entry.attempts <= 3
    assert entry.next_cycle - state.cycle <= 2


@pytest.mark.parametrize("action", ["merge", "deploy", "release", "credentials", "protected_data"])
def test_module_has_no_actuator_or_sensitive_access_surface(action: str) -> None:
    import skcapstone.link_cycle as module

    assert not hasattr(module, action)
