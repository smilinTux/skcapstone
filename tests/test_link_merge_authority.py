"""Governed source-only Link merge eligibility tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from skcapstone.link_merge_authority import (
    REQUIRED_GITHUB_CHECKS,
    REQUIRED_LOCAL_CHECKS,
    GitHubCheck,
    IndependentReview,
    LocalPreflight,
    MergeCandidate,
    evaluate_link_merge,
)

HEAD, BASE, TREE, DIFF = "a" * 40, "b" * 40, "c" * 40, "d" * 64
LOCAL = tuple(sorted(REQUIRED_LOCAL_CHECKS))


def receipt(tmp_path: Path, **changes: object) -> LocalPreflight:
    unsigned = {
        "schema": "skfleet.local-ci-preflight/v1",
        "repository": "x",
        "base": BASE,
        "head": HEAD,
        "tree": TREE,
        "paths": ["a.py"],
        "diff_sha256": DIFF,
        "checks": [
            {
                "name": n,
                "environment": n,
                "status": "completed",
                "conclusion": "success",
                "exit_code": 0,
                "elapsed_ms": 1,
            }
            for n in LOCAL
        ],
        "state": "PASS",
    }
    unsigned.update(changes)
    data = {
        **unsigned,
        "digest": hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = tmp_path / f"receipt-{len(list(tmp_path.iterdir()))}.json"
    path.write_bytes(raw)
    return LocalPreflight(str(path), hashlib.sha256(raw).hexdigest())


def candidate(tmp_path: Path, **changes: object) -> MergeCandidate:
    values = dict(
        repository="org/repo",
        number=1,
        title="docs repair",
        categories=("documentation",),
        head_sha=HEAD,
        base_sha=BASE,
        tree_sha=TREE,
        paths=("a.py",),
        diff_sha256=DIFF,
        author="mero",
        mergeable=True,
        github_checks=tuple(
            GitHubCheck(name, "completed", "success") for name in REQUIRED_GITHUB_CHECKS
        ),
        review=IndependentReview("reviewer", "PASS", HEAD, "e" * 64),
        local_preflight=receipt(tmp_path),
        lineage_outcomes=("PASS",),
    )
    values.update(changes)
    return MergeCandidate(**values)  # type: ignore[arg-type]


def test_valid_receipt_and_terminal_checks_are_eligible(tmp_path: Path) -> None:
    assert evaluate_link_merge(candidate(tmp_path)).eligible


@pytest.mark.parametrize("reviewer", ["link", "seat-link", "pi-link-chiap08-card", "LINK"])
def test_link_cannot_review_its_own_queue(tmp_path: Path, reviewer: str) -> None:
    review = IndependentReview(reviewer, "PASS", HEAD, "e" * 64)
    assert "reviewer-is-link" in evaluate_link_merge(candidate(tmp_path, review=review)).failures


def test_pending_github_check_fails_closed(tmp_path: Path) -> None:
    checks = tuple(
        GitHubCheck(
            name,
            "in_progress" if name == "build" else "completed",
            "" if name == "build" else "success",
        )
        for name in REQUIRED_GITHUB_CHECKS
    )
    assert (
        "github-check-not-successful"
        in evaluate_link_merge(candidate(tmp_path, github_checks=checks)).failures
    )


def test_tampered_receipt_fails_closed(tmp_path: Path) -> None:
    item = candidate(tmp_path)
    Path(item.local_preflight.receipt_path).write_text("{}")  # type: ignore[union-attr]
    assert "invalid-local-preflight-receipt" in evaluate_link_merge(item).failures


def test_receipt_candidate_mismatch_fails_closed(tmp_path: Path) -> None:
    local = receipt(tmp_path, head="f" * 40)
    assert (
        "invalid-local-preflight-receipt"
        in evaluate_link_merge(candidate(tmp_path, local_preflight=local)).failures
    )


def test_completed_failure_is_not_success(tmp_path: Path) -> None:
    checks = tuple(
        GitHubCheck(name, "completed", "failure" if name == "build" else "success")
        for name in REQUIRED_GITHUB_CHECKS
    )
    assert (
        "github-check-not-successful"
        in evaluate_link_merge(candidate(tmp_path, github_checks=checks)).failures
    )


def test_callers_cannot_reduce_required_check_sets(tmp_path: Path) -> None:
    local = receipt(tmp_path, checks=[])
    decision = evaluate_link_merge(candidate(tmp_path, local_preflight=local, github_checks=()))
    assert "missing-github-check" in decision.failures
    assert "invalid-local-preflight-receipt" in decision.failures
