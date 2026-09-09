"""Governed source-only Link merge eligibility tests."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.link_merge_authority import (
    CardStoreReview,
    IndependentReview,
    MergeCandidate,
    ProtectedMergeReadback,
    authorize_link_merge,
    evaluate_link_merge,
)

HEAD = "a" * 40


def candidate(**changes: object) -> MergeCandidate:
    values = {
        "repository": "smilinTux/skcapstone",
        "number": 338,
        "title": "docs: clarify card authoring",
        "categories": ("documentation",),
        "head_sha": HEAD,
        "author": "mero",
        "mergeable": True,
        "failed_checks": 0,
        "review": IndependentReview("reviewer", "PASS", HEAD, "e" * 64),
        "lineage_outcomes": ("PASS_FOR_REVIEW", "PASS"),
    }
    values.update(changes)
    return MergeCandidate(**values)  # type: ignore[arg-type]


def test_exact_head_independent_pass_is_eligible_and_evidenced() -> None:
    decision = evaluate_link_merge(candidate())

    assert decision.eligible
    assert decision.escalation is None
    assert f"head={HEAD}" in decision.evidence
    assert len(decision.evidence_sha256) == 64


@pytest.mark.parametrize(
    ("changes", "failure"),
    [
        ({"head_sha": "not-a-sha"}, "invalid-exact-head"),
        ({"mergeable": False}, "not-mergeable"),
        ({"failed_checks": 1}, "failed-checks"),
        ({"author": "pi-link-chiap08-card"}, "authored-by-seat-link"),
        ({"review": None}, "missing-independent-pass"),
        (
            {"review": IndependentReview("reviewer", "PASS", "b" * 40, "e" * 64)},
            "review-head-mismatch",
        ),
        (
            {"review": IndependentReview("mero", "PASS", HEAD, "e" * 64)},
            "reviewer-is-author",
        ),
        (
            {"review": IndependentReview("  ", "PASS", HEAD, "e" * 64)},
            "missing-reviewer-identity",
        ),
        (
            {"review": IndependentReview("reviewer", "PASS", "not-a-sha", "e" * 64)},
            "invalid-review-head",
        ),
        (
            {"review": IndependentReview("reviewer", "PASS", HEAD, "e")},
            "invalid-review-evidence",
        ),
        ({"lineage_outcomes": ("PASS", "BLOCKED|needs repair")}, "unresolved-lineage"),
    ],
)
def test_every_failed_gate_escalates_to_chef(changes: dict[str, object], failure: str) -> None:
    decision = evaluate_link_merge(candidate(**changes))

    assert not decision.eligible
    assert failure in decision.failures
    assert decision.escalation == "Chef"


@pytest.mark.parametrize(
    "value",
    [
        "CapAuth policy",
        "credential rotation",
        "custody",
        "issuer",
        "secret scan",
        "key handling",
        "rollback",
        "deploy",
        "production",
        "release notes",
        "migration",
    ],
)
def test_sensitive_title_or_category_is_never_eligible(value: str) -> None:
    assert "sensitive-class" in evaluate_link_merge(candidate(categories=(value,))).failures


def test_decision_is_deterministic_and_exposes_no_actuator() -> None:
    first = evaluate_link_merge(candidate())
    second = evaluate_link_merge(candidate())

    assert first == second
    assert not hasattr(first, "merge")


def _fenced_candidate(**changes: object) -> MergeCandidate:
    values = {
        "repository": "smilinTux/skcapstone",
        "number": 582,
        "title": "source lifecycle repair",
        "categories": ("lifecycle",),
        "head_sha": HEAD,
        "author": "producer",
        "mergeable": True,
        "failed_checks": 0,
        "review": IndependentReview("seraph", "PASS", HEAD, "e" * 64),
        "protected_base_sha": "b" * 40,
        "tree_sha": "t" * 40,
        "patch_sha256": "p" * 64,
        "required_checks": ("unit", "integration"),
        "source_card": "source-582",
        "source_generation": "g" * 64,
    }
    values.update(changes)
    return MergeCandidate(**values)  # type: ignore[arg-type]


def _review(**changes: object) -> CardStoreReview:
    values = {
        "review_card_id": "review-582",
        "source_card": "source-582",
        "source_generation": "g" * 64,
        "base_sha": "b" * 40,
        "head_sha": HEAD,
        "tree_sha": "t" * 40,
        "patch_sha256": "p" * 64,
        "status": "DONE",
        "verdict": "PASS",
        "reviewer_identity": "seraph",
        "reviewer_binding": ("seraph", "chiap02", "review", "/review"),
        "producer_binding": ("producer", "chiap01", "source", "/source"),
        "evidence_sha256": "e" * 64,
        "materialized_at": "2026-09-09T13:46:45Z",
        "terminalized_at": "2026-09-09T13:53:22Z",
    }
    values.update(changes)
    return CardStoreReview(**values)  # type: ignore[arg-type]


def _authorize(tmp_path: Path, **changes: object):
    review_fields = {
        "reviewer_binding",
        "merged_at",
        "terminalized_at",
        "materialized_at",
        "status",
        "verdict",
    }
    candidate_value = _fenced_candidate(
        **{key: value for key, value in changes.items() if key not in review_fields}
    )
    return authorize_link_merge(
        candidate_value,
        home=tmp_path,
        source_card="source-582",
        review_card="review-582",
        read_protected=lambda: ProtectedMergeReadback(
            base_sha="b" * 40,
            head_sha=HEAD,
            tree_sha="t" * 40,
            patch_sha256="p" * 64,
            checks={"unit": "SUCCESS", "integration": "SUCCESS"},
        ),
        read_review=lambda _store: _review(
            **{key: value for key, value in changes.items() if key in review_fields}
        ),
        now="2026-09-09T13:54:00Z",
    )


def _cards(tmp_path: Path) -> None:
    store = CardStore(tmp_path)
    store.create(CardCore(id="source-582", title="source", created_by="producer"))
    store.create(
        CardCore(
            id="review-582",
            title="[REVIEW] PR582",
            created_by="link",
            initial_labels=["review", "parent-source-582"],
        )
    )


def test_final_fence_rereads_exact_state_and_seals_receipt(tmp_path: Path) -> None:
    _cards(tmp_path)
    decision = _authorize(tmp_path)
    assert decision.eligible
    assert "base=" + "b" * 40 in decision.evidence
    assert len(decision.evidence_sha256) == 64


@pytest.mark.parametrize(
    ("change", "failure"),
    [
        ({"tree_sha": "x" * 40}, "candidate-tree-changed"),
        ({"patch_sha256": "x" * 64}, "candidate-patch-changed"),
        ({"protected_base_sha": "x" * 40}, "protected-base-changed"),
        (
            {"reviewer_binding": ("producer", "chiap01", "source", "/source")},
            "reviewer-producer-binding-mismatch",
        ),
        ({"merged_at": "2026-09-09T13:55:00Z"}, "post-merge-review"),
        ({"terminalized_at": "2026-09-09T13:46:01Z"}, "review-timestamp-order-invalid"),
    ],
)
def test_final_fence_rejects_stale_or_post_merge_review(
    tmp_path: Path, change: dict[str, object], failure: str
) -> None:
    _cards(tmp_path)
    decision = _authorize(tmp_path, **change)
    assert not decision.eligible
    assert failure in decision.failures


def test_final_fence_rejects_missing_or_nonterminal_cardstore_pass(tmp_path: Path) -> None:
    _cards(tmp_path)
    for review in (None, _review(status="REVIEW"), _review(verdict="BLOCKED")):
        decision = authorize_link_merge(
            _fenced_candidate(),
            home=tmp_path,
            source_card="source-582",
            review_card="review-582",
            read_protected=lambda: ProtectedMergeReadback(
                "b" * 40, HEAD, "t" * 40, "p" * 64, {"unit": "SUCCESS", "integration": "SUCCESS"}
            ),
            read_review=lambda _store, review=review: review,
            now="2026-09-09T13:54:00Z",
        )
        assert not decision.eligible
        assert (
            "missing-terminal-cardstore-pass" in decision.failures
            or "review-not-terminal-pass" in decision.failures
        )


def test_pr582_merge_cannot_beat_review_terminalization(tmp_path: Path) -> None:
    _cards(tmp_path)
    entered = Event()
    release = Event()

    def reread() -> ProtectedMergeReadback:
        entered.set()
        release.wait(2)
        return ProtectedMergeReadback(
            "b" * 40, HEAD, "t" * 40, "p" * 64, {"unit": "SUCCESS", "integration": "SUCCESS"}
        )

    result: list = []
    worker = Thread(
        target=lambda: result.append(
            authorize_link_merge(
                _fenced_candidate(),
                home=tmp_path,
                source_card="source-582",
                review_card="review-582",
                read_protected=reread,
                read_review=lambda _store: _review(status="REVIEW"),
                now="2026-09-09T13:54:00Z",
            )
        )
    )
    worker.start()
    assert entered.wait(1)
    terminalized = Event()

    def terminalize() -> None:
        CardStore(tmp_path).append_event("review-582", "complete", "seraph")
        terminalized.set()

    terminalizer = Thread(target=terminalize)
    terminalizer.start()
    assert not terminalized.wait(0.1)
    release.set()
    worker.join(2)
    terminalizer.join(2)
    assert result and not result[0].eligible
    assert "review-not-terminal-pass" in result[0].failures
    assert terminalized.is_set()
