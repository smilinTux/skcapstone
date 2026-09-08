"""Fail-closed, source-only eligibility decisions for Link PR merges."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_SENSITIVE = re.compile(
    r"(capauth|credential|custody|issuer|secret|\bkey\b|rollback|"
    r"deploy|production|release|migrat)",
    re.IGNORECASE,
)
_UNRESOLVED = re.compile(r"^\s*(FAIL|BLOCKED)\b", re.IGNORECASE)
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)
REQUIRED_GITHUB_CHECKS = frozenset(
    {
        "build",
        "docs / docs-check",
        "gitleaks",
        "lint",
        "provider tests (cloud)",
        "provider tests (docker)",
        "shim-imports",
        "unit tests (py3.11)",
        "unit tests (py3.12)",
    }
)
REQUIRED_LOCAL_CHECKS = frozenset(
    {
        "scope/diff",
        "lint/black-26.5.1",
        "lint/ruff-0.15.4",
        "docs/changelog",
        "secret/gitleaks-8.28.0",
        "imports/shims",
        "unit/python-3.11",
        "unit/python-3.12",
        "provider/cloud-python-3.12",
        "provider/docker-python-3.12",
        "package/build-twine-python-3.12",
    }
)


@dataclass(frozen=True)
class IndependentReview:
    """Review evidence bound to one exact PR head."""

    reviewer: str
    verdict: str
    head_sha: str
    evidence_sha256: str


@dataclass(frozen=True)
class LocalPreflight:
    """Terminal local CI receipt bound to the exact candidate head."""

    receipt_path: str
    evidence_sha256: str


@dataclass(frozen=True)
class GitHubCheck:
    """One required GitHub check with distinct lifecycle and verdict."""

    context: str
    status: str
    conclusion: str


@dataclass(frozen=True)
class MergeCandidate:
    """Closed input set for one Link merge eligibility decision."""

    repository: str
    number: int
    title: str
    categories: tuple[str, ...]
    head_sha: str
    base_sha: str
    tree_sha: str
    paths: tuple[str, ...]
    diff_sha256: str
    author: str
    mergeable: bool
    github_checks: tuple[GitHubCheck, ...]
    review: IndependentReview | None
    local_preflight: LocalPreflight | None
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
    preflight = candidate.local_preflight
    author = candidate.author.strip().lower()

    if not _GIT_SHA.fullmatch(candidate.head_sha):
        failures.append("invalid-exact-head")
    if not _GIT_SHA.fullmatch(candidate.base_sha) or not _GIT_SHA.fullmatch(candidate.tree_sha):
        failures.append("invalid-candidate-identity")
    if not _SHA256.fullmatch(candidate.diff_sha256):
        failures.append("invalid-candidate-diff")
    if not candidate.mergeable:
        failures.append("not-mergeable")
    github = {check.context: check for check in candidate.github_checks}
    if len(github) != len(candidate.github_checks):
        failures.append("duplicate-github-check")
    for context in REQUIRED_GITHUB_CHECKS:
        check = github.get(context)
        if check is None:
            failures.append("missing-github-check")
        elif check.status.casefold() != "completed" or check.conclusion.casefold() != "success":
            failures.append("github-check-not-successful")
    receipt_digest = ""
    if preflight is None:
        failures.append("missing-local-preflight")
    else:
        if not _SHA256.fullmatch(preflight.evidence_sha256):
            failures.append("invalid-local-preflight-evidence")
        try:
            raw = Path(preflight.receipt_path).read_bytes()
            if hashlib.sha256(raw).hexdigest() != preflight.evidence_sha256:
                raise ValueError("receipt hash mismatch")
            receipt = json.loads(raw)
            receipt_digest = str(receipt.pop("digest"))
            canonical = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
            if hashlib.sha256(canonical).hexdigest() != receipt_digest:
                raise ValueError("receipt digest mismatch")
            if receipt.get("schema") != "skfleet.local-ci-preflight/v1":
                raise ValueError("receipt schema mismatch")
            expected = {
                "base": candidate.base_sha,
                "head": candidate.head_sha,
                "tree": candidate.tree_sha,
                "paths": list(candidate.paths),
                "diff_sha256": candidate.diff_sha256,
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError("receipt candidate mismatch")
            checks = receipt.get("checks")
            if not isinstance(checks, list) or {item.get("name") for item in checks} != set(
                REQUIRED_LOCAL_CHECKS
            ):
                raise ValueError("receipt checks mismatch")
            if receipt.get("state") != "PASS" or any(
                item.get("status") != "completed"
                or item.get("conclusion") != "success"
                or item.get("exit_code") != 0
                for item in checks
            ):
                raise ValueError("receipt checks not successful")
        except (OSError, ValueError, TypeError, KeyError, AttributeError, json.JSONDecodeError):
            failures.append("invalid-local-preflight-receipt")
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
        reviewer = review.reviewer.strip().lower()
        if reviewer == author:
            failures.append("reviewer-is-author")
        if reviewer in {"link", "seat-link"} or reviewer.startswith(("link-", "pi-link-")):
            failures.append("reviewer-is-link")
        if not _SHA256.fullmatch(review.evidence_sha256):
            failures.append("invalid-review-evidence")

    evidence = (
        f"pr={candidate.repository}#{candidate.number}",
        f"head={candidate.head_sha}",
        f"mergeable={str(candidate.mergeable).lower()}",
        f"github_checks={len(candidate.github_checks)}",
        f"local_preflight={preflight.evidence_sha256 if preflight else ''}",
        f"local_preflight_digest={receipt_digest}",
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
