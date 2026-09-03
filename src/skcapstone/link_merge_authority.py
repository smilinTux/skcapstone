"""Fail-closed, source-only eligibility decisions for Link PR merges."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass

_SENSITIVE = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)",
    re.IGNORECASE,
)
_UNRESOLVED = re.compile(r"^\s*(FAIL|BLOCKED)\b", re.IGNORECASE)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


@dataclass(frozen=True)
class IndependentReview:
    """Review evidence bound to one exact PR head."""

    reviewer: str
    verdict: str
    head_sha: str
    evidence_sha256: str


@dataclass(frozen=True)
class MergeCandidate:
    """Closed input set for one Link merge eligibility decision."""

    repository: str
    number: int
    title: str
    categories: tuple[str, ...]
    head_sha: str
    author: str
    mergeable: bool
    failed_checks: int
    review: IndependentReview | None
    lineage_outcomes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergeDecision:
    """Evidence record only. This type deliberately has no merge actuator."""

    eligible: bool
    head_sha: str
    evidence: tuple[str, ...]
    failures: tuple[str, ...]
    escalation: str | None
    evidence_sha256: str


def evaluate_link_merge(candidate: MergeCandidate) -> MergeDecision:
    """Return Link merge eligibility and deterministic exact-head evidence."""
    failures: list[str] = []
    review = candidate.review
    author = candidate.author.strip().lower()

    if not _GIT_SHA.fullmatch(candidate.head_sha):
        failures.append("invalid-exact-head")
    if not candidate.mergeable:
        failures.append("not-mergeable")
    if candidate.failed_checks:
        failures.append("failed-checks")
    if author in {"link", "seat-link"} or author.startswith(("link-", "pi-link-")):
        failures.append("authored-by-seat-link")
    if _SENSITIVE.search(" ".join((candidate.title, *candidate.categories))):
        failures.append("sensitive-class")
    if any(_UNRESOLVED.search(outcome) for outcome in candidate.lineage_outcomes):
        failures.append("unresolved-lineage")
    if review is None:
        failures.append("missing-independent-pass")
    else:
        if not review.reviewer.strip():
            failures.append("missing-reviewer-identity")
        if review.verdict.strip().upper() != "PASS":
            failures.append("missing-independent-pass")
        if not _GIT_SHA.fullmatch(review.head_sha):
            failures.append("invalid-review-head")
        if review.head_sha != candidate.head_sha:
            failures.append("review-head-mismatch")
        if review.reviewer.strip().lower() == author:
            failures.append("reviewer-is-author")
        if not _SHA256.fullmatch(review.evidence_sha256):
            failures.append("invalid-review-evidence")

    evidence = (
        f"pr={candidate.repository}#{candidate.number}",
        f"head={candidate.head_sha}",
        f"mergeable={str(candidate.mergeable).lower()}",
        f"failed_checks={candidate.failed_checks}",
        f"review_evidence={review.evidence_sha256 if review else ''}",
    )
    payload = {
        "candidate": asdict(candidate),
        "eligible": not failures,
        "evidence": evidence,
        "failures": failures,
        "escalation": "Chef" if failures else None,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return MergeDecision(
        eligible=not failures,
        head_sha=candidate.head_sha,
        evidence=evidence,
        failures=tuple(failures),
        escalation="Chef" if failures else None,
        evidence_sha256=digest,
    )
