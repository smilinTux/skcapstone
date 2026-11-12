"""Governed source-only Link merge eligibility tests."""

from __future__ import annotations

import pytest

from skcapstone.link_merge_authority import (
    IndependentReview,
    MergeCandidate,
    ProtectedMergePolicy,
    evaluate_link_merge,
    resolve_protected_merge_method,
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
        "merge_policy": ProtectedMergePolicy(
            repository_allowed=("merge", "squash", "rebase"),
            branch_allowed=("squash",),
        ),
        "lineage_outcomes": ("PASS_FOR_REVIEW", "PASS"),
    }
    values.update(changes)
    return MergeCandidate(**values)  # type: ignore[arg-type]


def test_exact_head_independent_pass_is_eligible_and_evidenced() -> None:
    decision = evaluate_link_merge(candidate())

    assert decision.eligible
    assert decision.escalation is None
    assert decision.merge_method == "squash"
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


def test_merge_commit_rejection_selects_protected_squash() -> None:
    policy = ProtectedMergePolicy(
        repository_allowed=("merge", "squash"),
        branch_allowed=("squash",),
    )

    assert resolve_protected_merge_method(policy) == "squash"
    assert evaluate_link_merge(candidate(merge_policy=policy)).merge_method == "squash"


@pytest.mark.parametrize(
    ("allowed", "expected"),
    [
        (("merge", "rebase", "squash"), "squash"),
        (("merge", "rebase"), "rebase"),
        (("merge",), "merge"),
    ],
)
def test_protected_merge_method_preference_is_deterministic(
    allowed: tuple[str, ...], expected: str
) -> None:
    policy = ProtectedMergePolicy(
        repository_allowed=allowed,
        branch_allowed=tuple(reversed(allowed)),
    )

    assert resolve_protected_merge_method(policy) == expected


@pytest.mark.parametrize(
    ("policy", "failure"),
    [
        (None, "merge-policy-unavailable"),
        (
            ProtectedMergePolicy(repository_allowed=("merge",), branch_allowed=("squash",)),
            "no-protected-merge-method",
        ),
    ],
)
def test_merge_policy_fails_closed_before_any_actuator(
    policy: ProtectedMergePolicy | None, failure: str
) -> None:
    decision = evaluate_link_merge(candidate(merge_policy=policy))

    assert not decision.eligible
    assert decision.merge_method is None
    assert failure in decision.failures
