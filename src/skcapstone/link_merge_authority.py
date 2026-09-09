"""Fail-closed, source-only eligibility decisions for Link PR merges."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping

from skcoord.card_store import CardStore, card_mutation_lock

from .seat_boundaries import BoundaryError

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
    tree_sha: str | None = None
    patch_sha256: str | None = None
    protected_base_sha: str | None = None
    required_checks: tuple[str, ...] = ()
    source_card: str | None = None
    source_generation: str | None = None


@dataclass(frozen=True)
class ProtectedMergeReadback:
    """Exact protected-branch state reread immediately before a merge."""

    base_sha: str
    head_sha: str
    tree_sha: str
    patch_sha256: str
    checks: Mapping[str, str]
    mergeable: bool = True


@dataclass(frozen=True)
class CardStoreReview:
    """Terminal independent review state bound to one source candidate."""

    review_card_id: str
    source_card: str
    source_generation: str
    base_sha: str
    head_sha: str
    tree_sha: str
    patch_sha256: str
    status: str
    verdict: str
    reviewer_identity: str
    reviewer_binding: tuple[str, str, str, str]
    producer_binding: tuple[str, str, str, str]
    evidence_sha256: str
    materialized_at: str
    terminalized_at: str
    merged_at: str | None = None


@dataclass(frozen=True)
class MergeDecision:
    """Evidence record only. This type deliberately has no merge actuator."""

    eligible: bool
    head_sha: str
    evidence: tuple[str, ...]
    failures: tuple[str, ...]
    escalation: str | None
    evidence_sha256: str


@contextmanager
def merge_lifecycle_lock(home: Path, source_card: str, review_card: str) -> Iterator[None]:
    """Serialize merge authorization with review materialization and completion."""

    if not source_card or not review_card or source_card == review_card:
        raise BoundaryError("source and review cards must be distinct")
    with ExitStack() as stack:
        for card_id in sorted((source_card, review_card)):
            try:
                stack.enter_context(card_mutation_lock(home, card_id, artifact_neutral=True))
            except (OSError, ValueError, TimeoutError) as exc:
                raise BoundaryError("source or review CardStore card is unavailable") from exc
        yield


def authorize_link_merge(
    candidate: MergeCandidate,
    *,
    home: Path,
    source_card: str,
    review_card: str,
    read_protected: Callable[[], ProtectedMergeReadback],
    read_review: Callable[[CardStore], CardStoreReview | None],
    now: str,
) -> MergeDecision:
    """Atomically reread every merge predicate and return a sealed receipt."""

    with merge_lifecycle_lock(home, source_card, review_card):
        protected = read_protected()
        review = read_review(CardStore(home))
        failures: list[str] = []
        if protected.base_sha != candidate.protected_base_sha:
            failures.append("protected-base-changed")
        if protected.head_sha != candidate.head_sha:
            failures.append("candidate-head-changed")
        if protected.tree_sha != candidate.tree_sha:
            failures.append("candidate-tree-changed")
        if protected.patch_sha256 != candidate.patch_sha256:
            failures.append("candidate-patch-changed")
        if not protected.mergeable:
            failures.append("not-mergeable")
        if any(
            protected.checks.get(name) not in {"SUCCESS", "NEUTRAL", "SKIPPED"}
            for name in candidate.required_checks
        ):
            failures.append("required-ci-not-success")
        if review is None:
            failures.append("missing-terminal-cardstore-pass")
        else:
            if review.status.upper() != "DONE" or review.verdict.upper() != "PASS":
                failures.append("review-not-terminal-pass")
            if review.source_card != source_card or review.review_card_id != review_card:
                failures.append("source-review-binding-mismatch")
            if candidate.source_card is not None and review.source_card != candidate.source_card:
                failures.append("source-card-mismatch")
            if (
                candidate.source_generation is not None
                and review.source_generation != candidate.source_generation
            ):
                failures.append("source-generation-mismatch")
            if (review.base_sha, review.head_sha, review.tree_sha, review.patch_sha256) != (
                candidate.protected_base_sha,
                candidate.head_sha,
                candidate.tree_sha,
                candidate.patch_sha256,
            ):
                failures.append("review-artifact-binding-mismatch")
            if any(
                left.strip().lower() == right.strip().lower()
                for left, right in zip(review.reviewer_binding, review.producer_binding)
            ):
                failures.append("reviewer-producer-binding-mismatch")
            if review.reviewer_identity.strip().lower() in {
                "",
                candidate.author.strip().lower(),
                "link",
            }:
                failures.append("reviewer-not-independent")
            if review.merged_at is not None:
                failures.append("post-merge-review")
            if not (
                review.terminalized_at < now and review.materialized_at <= review.terminalized_at
            ):
                failures.append("review-timestamp-order-invalid")
        payload = {
            "candidate": asdict(candidate),
            "protected": asdict(protected),
            "review": asdict(review) if review else None,
            "now": now,
            "failures": failures,
        }
        evidence = (
            f"base={protected.base_sha}",
            f"head={protected.head_sha}",
            f"tree={protected.tree_sha}",
            f"patch={protected.patch_sha256}",
            f"review={review_card}",
            f"ci={json.dumps(dict(protected.checks), sort_keys=True)}",
        )
        return MergeDecision(
            eligible=not failures,
            head_sha=candidate.head_sha,
            evidence=evidence,
            failures=tuple(failures),
            escalation="Chef" if failures else None,
            evidence_sha256=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )


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
