"""Bounded, source-only PR triage for the Link integrator seat.

The module consumes already-authorized observations. It has no GitHub client,
credential reader, launcher, merger, deployer, or CardStore lifecycle mutation.
Its outputs are recommendations that Jarvis must fence against a fresh read.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping, Sequence

from .link_merge_authority import IndependentReview, MergeCandidate
from .seat_boundaries import BoundaryError, evaluate_merge_as_link

_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_REVIEW = frozenset({"PASS", "BLOCKED", "FAIL"})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class PullRequestObservation:
    """Immutable input for one open PR at one observed revision."""

    repository: str
    number: int
    title: str
    author: str
    head_sha: str
    base_sha: str
    ci_state: str
    conflict_state: str
    review_requests: tuple[str, ...]
    source_card: str | None
    card_generation: str | None
    observed_at: str
    active_review_card: str | None = None
    categories: tuple[str, ...] = ("source",)
    lineage_outcomes: tuple[str, ...] = ()

    def validate(self) -> None:
        required = (self.repository, self.title, self.author, self.observed_at)
        if self.number < 1 or any(not item.strip() for item in required):
            raise ValueError("complete PR identity and observation time are required")
        if not _GIT_SHA.fullmatch(self.head_sha) or not _GIT_SHA.fullmatch(self.base_sha):
            raise ValueError("head_sha and base_sha must be lowercase full Git SHAs")
        if bool(self.source_card) != bool(self.card_generation):
            raise ValueError("source card and card generation must be present together")
        if any(not item.strip() for item in self.review_requests):
            raise ValueError("review request identities cannot be blank")

    @property
    def key(self) -> str:
        return f"{self.repository}#{self.number}"

    @property
    def observation_revision(self) -> str:
        """Hash every inventory field, including exact source generation."""

        self.validate()
        return _digest(asdict(self))

    @property
    def clean(self) -> bool:
        return self.ci_state.lower() in {"success", "passing"} and self.conflict_state.lower() in {
            "clean",
            "mergeable",
        }

    @property
    def classification(self) -> str:
        if self.source_card is None:
            return "orphaned"
        if self.conflict_state.lower() not in {"clean", "mergeable"}:
            return "needs-work"
        if self.ci_state.lower() in {"stale", "unknown", "pending"}:
            return "stale"
        if self.ci_state.lower() not in {"success", "passing"}:
            return "needs-work"
        if self.review_requests or self.active_review_card:
            return "review-active"
        return "needs-reviewer"


@dataclass(frozen=True)
class ReviewerIdentity:
    """Eligibility facts needed to prove reviewer distinctness."""

    name: str
    identity: str
    host: str
    session: str
    workspace: str
    eligible: bool = True

    def distinct_key(self) -> tuple[str, str, str, str]:
        return (self.identity, self.host, self.session, self.workspace)


@dataclass(frozen=True)
class ProducerIdentity:
    identity: str
    host: str
    session: str
    workspace: str

    def distinct_key(self) -> tuple[str, str, str, str]:
        return (self.identity, self.host, self.session, self.workspace)


@dataclass(frozen=True)
class RevisionFencedReviewHandoff:
    """Advisory launch input. Only Jarvis may validate and execute it."""

    recommendation_id: str
    repository: str
    pr_number: int
    head_sha: str
    source_card: str
    card_generation: str
    review_card_id: str
    review_card_revision: str
    reviewer: str
    observation_revision: str
    recommender: str = "link"

    def as_event(self) -> dict[str, object]:
        payload = asdict(self)
        payload["schema"] = "skfleet.pr-review-handoff/v1"
        return payload


def recommend_one_reviewer(
    observation: PullRequestObservation,
    *,
    producer: ProducerIdentity,
    candidates: Sequence[ReviewerIdentity],
    review_card_id: str,
    review_card_revision: str,
) -> RevisionFencedReviewHandoff | None:
    """Return exactly one stable eligible reviewer, or no handoff.

    An existing request or active review suppresses output. Selection requires
    identity, host, session, and workspace all to differ from the producer and
    excludes Link itself.
    """

    if observation.classification != "needs-reviewer":
        return None
    if not review_card_id.strip() or not _SHA256.fullmatch(review_card_revision):
        raise BoundaryError("review card id and lowercase SHA-256 revision are required")
    selected = None
    producer_values = producer.distinct_key()
    for candidate in candidates:
        values = candidate.distinct_key()
        if (
            candidate.eligible
            and candidate.name.strip()
            and candidate.identity.strip().lower() != "link"
            and all(value.strip() for value in values)
            and all(left.lower() != right.lower() for left, right in zip(values, producer_values))
        ):
            selected = candidate
            break
    if selected is None:
        raise BoundaryError("no distinct eligible reviewer is available")
    revision = observation.observation_revision
    recommendation_id = _digest(
        [
            observation.key,
            observation.head_sha,
            observation.card_generation,
            revision,
            selected.name,
        ]
    )
    return RevisionFencedReviewHandoff(
        recommendation_id=recommendation_id,
        repository=observation.repository,
        pr_number=observation.number,
        head_sha=observation.head_sha,
        source_card=observation.source_card or "",
        card_generation=observation.card_generation or "",
        review_card_id=review_card_id,
        review_card_revision=review_card_revision,
        reviewer=selected.name.strip(),
        observation_revision=revision,
    )


def validate_handoff(
    handoff: RevisionFencedReviewHandoff,
    *,
    current_observation: PullRequestObservation,
    current_review_card_revision: str,
    active_review_card: str | None,
    used_recommendation_ids: set[str],
    actor: str,
) -> None:
    """Jarvis-side stale/replay fence before the existing claim and launcher."""

    if actor.strip().lower() != "jarvis":
        raise BoundaryError("only jarvis may validate a review handoff")
    if handoff.recommendation_id in used_recommendation_ids:
        raise BoundaryError("recommendation replay denied")
    if active_review_card:
        raise BoundaryError("an active review already exists")
    if current_observation.observation_revision != handoff.observation_revision:
        raise BoundaryError("PR observation revision changed")
    if current_observation.head_sha != handoff.head_sha:
        raise BoundaryError("PR head changed")
    if current_observation.card_generation != handoff.card_generation:
        raise BoundaryError("source card generation changed")
    if current_review_card_revision != handoff.review_card_revision:
        raise BoundaryError("review card revision changed")


@dataclass(frozen=True)
class TerminalReviewEvidence:
    review_card_id: str
    source_card: str
    card_generation: str
    head_sha: str
    reviewer: str
    verdict: str
    evidence_sha256: str

    def validate(self) -> None:
        if self.verdict.strip().upper() not in _TERMINAL_REVIEW:
            raise ValueError("review evidence must contain an explicit terminal verdict")
        if not _GIT_SHA.fullmatch(self.head_sha) or not _SHA256.fullmatch(self.evidence_sha256):
            raise ValueError("review evidence hashes are invalid")
        if any(
            not value.strip()
            for value in (
                self.review_card_id,
                self.source_card,
                self.card_generation,
                self.reviewer,
            )
        ):
            raise ValueError("review evidence identity is incomplete")


@dataclass(frozen=True)
class MergeEligibilityRecommendation:
    classification: str
    eligible: bool
    failures: tuple[str, ...]
    observation_revision: str
    evidence_sha256: str
    authority: str = "recommendation-only"


def join_review_evidence(
    observation: PullRequestObservation,
    evidence: TerminalReviewEvidence | None,
) -> MergeEligibilityRecommendation:
    """Join explicit evidence to exact head and generation, never lifecycle."""

    failures: list[str] = []
    independent = None
    if evidence is None:
        failures.append("missing-terminal-review-evidence")
    else:
        evidence.validate()
        if evidence.source_card != observation.source_card:
            failures.append("review-source-card-mismatch")
        if evidence.card_generation != observation.card_generation:
            failures.append("review-card-generation-mismatch")
        if evidence.head_sha != observation.head_sha:
            failures.append("review-head-mismatch")
        if not failures:
            independent = IndependentReview(
                evidence.reviewer,
                evidence.verdict.upper(),
                evidence.head_sha,
                evidence.evidence_sha256,
            )
    candidate = MergeCandidate(
        repository=observation.repository,
        number=observation.number,
        title=observation.title,
        categories=observation.categories,
        head_sha=observation.head_sha,
        author=observation.author,
        mergeable=observation.conflict_state.lower() in {"clean", "mergeable"},
        failed_checks=0 if observation.ci_state.lower() in {"success", "passing"} else 1,
        review=independent,
        lineage_outcomes=observation.lineage_outcomes,
    )
    decision = evaluate_merge_as_link("link", candidate)
    combined = tuple(dict.fromkeys([*failures, *decision.failures]))
    classification = "merge-eligible" if not combined else "not-merge-eligible"
    payload = {
        "classification": classification,
        "failures": combined,
        "observation_revision": observation.observation_revision,
        "review_evidence_sha256": evidence.evidence_sha256 if evidence else None,
        "authority": "recommendation-only",
    }
    return MergeEligibilityRecommendation(
        classification=classification,
        eligible=not combined,
        failures=combined,
        observation_revision=observation.observation_revision,
        evidence_sha256=_digest(payload),
    )


@dataclass(frozen=True)
class RetryEntry:
    revision: str
    attempts: int
    next_cycle: int


@dataclass(frozen=True)
class CycleState:
    cursor: int = 0
    cycle: int = 0
    entries: Mapping[str, RetryEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class CycleBatch:
    observations: tuple[PullRequestObservation, ...]
    state: CycleState


def next_batch(
    inventory: Iterable[PullRequestObservation],
    state: CycleState,
    *,
    limit: int,
    retry_keys: frozenset[str] = frozenset(),
    max_retries: int = 3,
    revisit_after_cycles: int = 3,
) -> CycleBatch:
    """Select a bounded circular batch with changed-state and retry priority."""

    if limit < 1 or max_retries < 1 or revisit_after_cycles < 1:
        raise ValueError("cycle bounds must be positive")
    rows = sorted(inventory, key=lambda item: (item.repository, item.number))
    for row in rows:
        row.validate()
    if not rows:
        return CycleBatch((), CycleState(0, state.cycle + 1, dict(state.entries)))
    cycle = state.cycle + 1
    start = state.cursor % len(rows)
    ordered = rows[start:] + rows[:start]
    changed = [
        row
        for row in ordered
        if state.entries.get(row.key, None) is None
        or state.entries[row.key].revision != row.observation_revision
    ]
    due = [
        row for row in ordered if row not in changed and state.entries[row.key].next_cycle <= cycle
    ]
    selected = (changed + due)[:limit]
    entries = dict(state.entries)
    for row in selected:
        previous = entries.get(row.key)
        retry = row.key in retry_keys
        attempts = (
            min(previous.attempts + 1, max_retries)
            if retry and previous and previous.revision == row.observation_revision
            else (1 if retry else 0)
        )
        delay = (
            min(2 ** max(attempts - 1, 0), revisit_after_cycles) if retry else revisit_after_cycles
        )
        if retry and attempts >= max_retries:
            delay = revisit_after_cycles
        entries[row.key] = RetryEntry(row.observation_revision, attempts, cycle + delay)
    cursor = (start + max(len(selected), 1)) % len(rows)
    return CycleBatch(tuple(selected), CycleState(cursor, cycle, entries))
