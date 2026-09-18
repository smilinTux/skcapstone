"""Fail-closed authority boundaries for lifecycle seats."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Collection, Mapping

from .card_store import CardStore
from .operator_authorization import AuthorizationEnvelope, verify_authorization

if TYPE_CHECKING:
    from .link_merge_authority import MergeCandidate, MergeDecision


class BoundaryError(ValueError):
    """Raised when a seat attempts an action outside its authority."""


class Seat(StrEnum):
    """Canonical operating seats that participate in coordination."""

    JARVIS = "jarvis"
    LINK = "link"
    MERO = "mero"
    SERAPH = "seraph"
    NIOBE = "niobe"
    TANK = "tank"
    ATLAS = "atlas"


class Action(StrEnum):
    """Actions whose seat ownership must remain explicit."""

    OBSERVE = "observe"
    RECOMMEND = "recommend"
    TRIAGE = "triage"
    ASSIGN_REVIEWER = "assign_reviewer"
    EVALUATE_MERGE = "evaluate_merge"
    MERGE = "merge"
    CLAIM = "claim"
    RELEASE = "release"
    LAUNCH = "launch"
    STOP = "stop"
    REASSIGN = "reassign"
    ROTATE = "rotate"
    REPAIR_WORKER = "repair_worker"
    DEPLOY = "deploy"
    ACTUATE_APPLICATION = "actuate_application"
    CREATE_CARD = "create_card"
    MOVE_CARD = "move_card"
    COMPLETE_CARD = "complete_card"
    RELEASE_ARTIFACT = "release_artifact"
    VERIFY = "verify"
    VOID_CARD = "void_card"
    DESCRIBE_CARD = "describe_card"
    LABEL_CARD = "label_card"
    LINK_CARD = "link_card"
    REPRIORITIZE_CARD = "reprioritize_card"
    AMEND_CRITERIA = "amend_criteria"
    AMEND_DEPENDENCIES = "amend_dependencies"
    SATISFY_GATE = "satisfy_gate"
    SCORE_CARD = "score_card"
    MAINTAIN_BOARD = "maintain_board"


JARVIS_DIRECT_ACTIONS = frozenset(
    {
        Action.CREATE_CARD,
        Action.CLAIM,
        Action.MOVE_CARD,
        Action.COMPLETE_CARD,
        Action.LAUNCH,
        Action.RELEASE,
        Action.STOP,
        Action.REASSIGN,
        Action.ROTATE,
        Action.REPAIR_WORKER,
        Action.VERIFY,
        Action.VOID_CARD,
        Action.DESCRIBE_CARD,
        Action.LABEL_CARD,
        Action.LINK_CARD,
        Action.REPRIORITIZE_CARD,
        Action.AMEND_CRITERIA,
        Action.AMEND_DEPENDENCIES,
        Action.SATISFY_GATE,
        Action.SCORE_CARD,
        Action.MAINTAIN_BOARD,
    }
)


_ALLOWED = {
    Seat.MERO: frozenset({Action.OBSERVE, Action.RECOMMEND, Action.CREATE_CARD}),
    Seat.LINK: frozenset(
        {
            Action.OBSERVE,
            Action.RECOMMEND,
            Action.TRIAGE,
            Action.ASSIGN_REVIEWER,
            Action.EVALUATE_MERGE,
            Action.MERGE,
            Action.CREATE_CARD,
        }
    ),
    Seat.SERAPH: frozenset({Action.OBSERVE, Action.CREATE_CARD}),
    Seat.NIOBE: frozenset(
        {
            Action.OBSERVE,
            Action.CLAIM,
            Action.RELEASE,
            Action.LAUNCH,
            Action.STOP,
            Action.REASSIGN,
            Action.ROTATE,
            Action.REPAIR_WORKER,
            Action.CREATE_CARD,
        }
    ),
    Seat.TANK: frozenset({Action.OBSERVE, Action.DEPLOY, Action.CREATE_CARD}),
    Seat.ATLAS: frozenset({Action.OBSERVE, Action.ACTUATE_APPLICATION, Action.CREATE_CARD}),
    # Jarvis is not scheduled as a recurring seat. These capabilities remain
    # available only for explicit Casey-directed emergency assistance.
    Seat.JARVIS: frozenset(
        {
            Action.OBSERVE,
            Action.CLAIM,
            Action.RELEASE,
            Action.LAUNCH,
            Action.STOP,
            Action.REASSIGN,
            Action.ROTATE,
            Action.REPAIR_WORKER,
            Action.CREATE_CARD,
            Action.MOVE_CARD,
            Action.COMPLETE_CARD,
            Action.MERGE,
            Action.DEPLOY,
            Action.RELEASE_ARTIFACT,
            Action.VERIFY,
            Action.ACTUATE_APPLICATION,
            Action.VOID_CARD,
            Action.DESCRIBE_CARD,
            Action.LABEL_CARD,
            Action.LINK_CARD,
            Action.REPRIORITIZE_CARD,
            Action.AMEND_CRITERIA,
            Action.AMEND_DEPENDENCIES,
            Action.SATISFY_GATE,
            Action.SCORE_CARD,
            Action.MAINTAIN_BOARD,
        }
    ),
}
_FLEET_MUTATIONS = frozenset(
    {
        Action.CLAIM,
        Action.RELEASE,
        Action.LAUNCH,
        Action.STOP,
        Action.REASSIGN,
        Action.ROTATE,
        Action.REPAIR_WORKER,
    }
)
_SHA256 = re.compile(r"[0-9a-f]{64}")

#: Every coordination-board verb that mutates state. Consulted by the coverage
#: test as well as the decision function: a new mutating verb belongs here and
#: in exactly one seat/class grant, or it is refused for every seat.
COORD_MUTATIONS = frozenset(
    {
        Action.CREATE_CARD,
        Action.CLAIM,
        Action.RELEASE,
        Action.MOVE_CARD,
        Action.COMPLETE_CARD,
        Action.VOID_CARD,
        Action.DESCRIBE_CARD,
        Action.LABEL_CARD,
        Action.LINK_CARD,
        Action.REPRIORITIZE_CARD,
        Action.AMEND_CRITERIA,
        Action.AMEND_DEPENDENCIES,
        Action.SATISFY_GATE,
        Action.SCORE_CARD,
        Action.MAINTAIN_BOARD,
    }
)

_SEAT_CARD_WORK = frozenset(
    {
        Action.CLAIM,
        Action.RELEASE,
        Action.MOVE_CARD,
        Action.COMPLETE_CARD,
        Action.LINK_CARD,
        Action.VOID_CARD,
        Action.DESCRIBE_CARD,
        Action.LABEL_CARD,
        Action.AMEND_CRITERIA,
        Action.AMEND_DEPENDENCIES,
        Action.SATISFY_GATE,
    }
)

#: NAMED ALLOWANCE, not an accident. The seat charters read as if the
#: lifecycle seats never touch card lifecycle verbs, but the estate depends on
#: exactly that: each recurring seat cycle claims, moves, and completes its own
#: seat-labeled cards under the seat identity. Measured on chi (per-writer
#: event shards, 14 days to 2026-09-18): link 118 move / 16 claim / 14
#: complete / 3 release_claim / 6 void; seraph 407 move / 282 claim / 147
#: complete / 67 release_claim; tank 10 move / 7 claim (still writing on
#: 2026-09-16 despite the paper retirement); atlas and niobe single digits of
#: claim / move / complete / void, all ongoing. Refusing these silently would
#: stop the review and verification lanes, so the deviation is granted here,
#: by name, where a reader can find and revoke it. Mero is deliberately
#: absent: the Overseer is read-only and its mutations were the incident that
#: forced this table (PR 766).
SEAT_CARD_LIFECYCLE_ALLOWANCES: Mapping[Seat, frozenset[Action]] = {
    Seat.LINK: _SEAT_CARD_WORK | {Action.REPRIORITIZE_CARD},
    Seat.SERAPH: _SEAT_CARD_WORK,
    Seat.NIOBE: _SEAT_CARD_WORK | {Action.REPRIORITIZE_CARD, Action.MAINTAIN_BOARD},
    Seat.ATLAS: _SEAT_CARD_WORK,
    Seat.TANK: _SEAT_CARD_WORK | {Action.REPRIORITIZE_CARD},
}

#: The one coordination capability table: base seat authority plus the named
#: card-lifecycle allowances above. Every coord mutation entrypoint consults
#: this through require_coord_authority and nothing else.
COORD_SEAT_CAPABILITIES: Mapping[Seat, frozenset[Action]] = {
    seat: _ALLOWED[seat] | SEAT_CARD_LIFECYCLE_ALLOWANCES.get(seat, frozenset()) for seat in Seat
}

#: Humans and standing personal agents. They are not seats: no charter bounds
#: them, and the board is theirs to operate. Bare identities only; a compound
#: identity is classified by the delegate grammar below.
OPERATOR_PRINCIPALS = frozenset(
    {"chef", "casey", "david", "human", "lumina", "opus", "kimi", "codex", "skuser01"}
)

#: Bare (unhyphenated) writer identities used by repo-owned automation and
#: fixture tooling, measured live on chi: mcp (181 describe/14d), import,
#: reconcile, coord, pi. Compound automation names (archive-done, stale-sweep,
#: fleet-liveness-reaper, lifecycle-reconciler, ...) match the delegate
#: grammar instead and need no listing.
SYSTEM_WRITERS = frozenset({"mcp", "import", "reconcile", "coord", "pi"})


class CoordActorClass(StrEnum):
    """How a coordination-board writer identity is classified."""

    SEAT = "seat"
    OPERATOR = "operator"
    DELEGATE = "delegate"
    TOOL = "tool"
    UNKNOWN = "unknown"


def classify_coord_actor(actor: str) -> tuple[CoordActorClass, str]:
    """Classify a coord writer identity. UNKNOWN means the gate refuses it.

    The grammar, in precedence order:

    - empty string: TOOL. The CLI's identityless default; the event log stamps
      the hostname as writer. Refusing it would refuse every plain operator
      invocation at a terminal, so it is a named allowance, not an oversight.
    - an exact seat name (case-insensitive, with capauth:/fqid spellings
      resolved to their principal): SEAT, bound by COORD_SEAT_CAPABILITIES.
    - a known human or standing personal agent: OPERATOR.
    - a hyphenated compound identity (pi-codex-chiap04-<cardid>,
      cursor-w73-live, archive-done, lifecycle-reconciler, ...) or a listed
      bare system writer: DELEGATE. A seat-prefixed compound (pi-mero-...) is
      a delegated worker, not the seat; the charter binds the exact seat
      identity, the precedent set by PR 766.
    - anything else: UNKNOWN.
    """

    normalized = actor.strip().casefold()
    if normalized.startswith("capauth:"):
        normalized = normalized.removeprefix("capauth:")
    normalized = normalized.split("@", 1)[0]
    if not normalized:
        return CoordActorClass.TOOL, normalized
    try:
        seat = Seat(normalized)
    except ValueError:
        pass
    else:
        return CoordActorClass.SEAT, seat.value
    if normalized in OPERATOR_PRINCIPALS:
        return CoordActorClass.OPERATOR, normalized
    if normalized in SYSTEM_WRITERS or "-" in normalized:
        return CoordActorClass.DELEGATE, normalized
    return CoordActorClass.UNKNOWN, normalized


def require_coord_authority(actor: str, action: Action) -> None:
    """The single decision function for every coord mutation entrypoint.

    One table (COORD_SEAT_CAPABILITIES), consulted for every actor, failing
    closed: a seat gets exactly its row, an identity the table and grammar do
    not know is refused outright. Callers hold no policy of their own; per
    SKWORLD_AUTHORIZATION_STANDARD they are thin PEPs and this is the local
    PDP until coord subjects carry capauth credentials to feed
    capauth.authz.decide.
    """

    actor_class, principal = classify_coord_actor(actor)
    if actor_class is CoordActorClass.UNKNOWN:
        raise BoundaryError(
            f"unknown coordination identity: {actor!r} is not a seat, a known "
            "operator, or a delegated worker/system identity, and the board "
            "fails closed"
        )
    if actor_class is not CoordActorClass.SEAT:
        return
    seat = Seat(principal)
    if action not in COORD_SEAT_CAPABILITIES[seat]:
        raise BoundaryError(f"{seat.value} is not authorized for {action.value}")


def canonical_human_principal(identity: str) -> str:
    """Normalize a CapAuth URI, handle, or local name to its human principal."""

    normalized = identity.strip().casefold()
    if normalized.startswith("capauth:"):
        normalized = normalized.removeprefix("capauth:")
    if ":" in normalized:
        return ""
    return normalized.split("@", 1)[0]


def require_authority(
    actor: str,
    action: Action,
    *,
    fenced_system_actors: Collection[str] = (),
) -> None:
    """Reject actions not owned by the named seat or fenced system actor."""

    normalized = actor.strip().lower()
    if action in _FLEET_MUTATIONS and normalized in fenced_system_actors:
        return
    try:
        seat = Seat(normalized)
    except ValueError as exc:
        raise BoundaryError(f"unknown or unfenced actor: {actor}") from exc
    if action not in _ALLOWED[seat]:
        raise BoundaryError(f"{seat.value} is not authorized for {action.value}")
    if seat is Seat.JARVIS and action not in JARVIS_DIRECT_ACTIONS | {Action.OBSERVE}:
        raise BoundaryError(
            f"jarvis requires a verified signed Casey direction for {action.value}"
        )


def verify_casey_direction(
    actor: str,
    action: Action,
    *,
    envelope: AuthorizationEnvelope | None,
    target: str,
    change_id: str,
    scope: str,
    public_key_armor: str,
    expected_fingerprint: str,
    verifier: Callable[[bytes, str, str], bool],
) -> None:
    """Verify one signed Casey direction bound to an exact emergency action.

    This is the shared mutation boundary used by Jarvis emergency entrypoints.
    The envelope is deliberately not consumed here: one direction may authorize
    a bounded workflow containing multiple exact operations, and every operation
    still verifies its own action, target, change, scope, lifetime, and signature.
    """

    normalized = actor.strip().lower()
    if normalized != Seat.JARVIS:
        require_authority(actor, action)
        return
    if envelope is None:
        raise BoundaryError(f"jarvis requires a signed Casey direction for {action.value}")
    if canonical_human_principal(envelope.issuer) != "casey":
        raise BoundaryError("Jarvis emergency direction issuer must be Casey")
    if not expected_fingerprint or envelope.issuer_fingerprint != expected_fingerprint:
        raise BoundaryError("Jarvis emergency direction signer does not match Casey")
    try:
        verify_authorization(
            envelope,
            public_key_armor=public_key_armor,
            verifier=verifier,
            expected_action=f"jarvis.{action.value}",
            expected_target=target,
            expected_change_id=change_id,
            expected_scope=scope,
        )
    except ValueError as exc:
        raise BoundaryError(str(exc)) from exc


def canonical_principal(identity: str) -> str:
    """Return the stable lifecycle principal behind a display identity."""

    normalized = identity.strip().casefold().replace("_", "-")
    if normalized.startswith("pi-"):
        normalized = normalized[3:]
    for seat in Seat:
        if normalized == seat.value or normalized.startswith(f"{seat.value}-"):
            return seat.value
    return normalized


def assign_distinct_reviewer(*, author: str, assigner: str, candidates: Collection[str]) -> str:
    """Choose the first stable reviewer distinct from author and Link."""

    require_authority(assigner, Action.ASSIGN_REVIEWER)
    excluded = {canonical_principal(author), canonical_principal(assigner)}
    for candidate in candidates:
        normalized = candidate.strip()
        if normalized and canonical_principal(normalized) not in excluded:
            return normalized
    raise BoundaryError("no distinct reviewer is available")


@dataclass(frozen=True)
class DispatchRecommendation:
    """Advisory observation that the dispatcher may evaluate and act upon."""

    card_id: str
    recommendation_id: str
    recommender: str
    observed_at: datetime
    observed_claim_owner: str | None
    observed_claim_revision: str | None
    observed_process: Mapping[str, object]
    reason: str
    evidence_sha256: str

    def validate(self) -> None:
        """Validate the closed recommendation envelope."""

        require_authority(self.recommender, Action.RECOMMEND)
        required = (self.card_id, self.recommendation_id, self.reason)
        if any(not value.strip() for value in required):
            raise BoundaryError("recommendation identifiers and reason are required")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise BoundaryError("observed_at must be timezone aware")
        if not _SHA256.fullmatch(self.evidence_sha256):
            raise BoundaryError("evidence_sha256 must be lowercase SHA-256")

    def as_event(self) -> dict[str, object]:
        """Return the typed payload for append-only CardStore emission."""

        self.validate()
        return {
            "schema": "skfleet.dispatch-recommendation/v1",
            "card_id": self.card_id,
            "recommendation_id": self.recommendation_id,
            "recommender": self.recommender,
            "observed_at": self.observed_at.isoformat(),
            "observed_claim_owner": self.observed_claim_owner,
            "observed_claim_revision": self.observed_claim_revision,
            "observed_process": dict(self.observed_process),
            "reason": self.reason,
            "evidence_sha256": self.evidence_sha256,
        }


def append_recommendation(home: Path, recommendation: DispatchRecommendation) -> dict[str, object]:
    """Append one typed recommendation idempotently to its card event log."""

    payload = recommendation.as_event()
    payload.pop("card_id")
    payload.pop("recommender")
    return CardStore(home).append_event(
        recommendation.card_id,
        "dispatch_recommendation",
        recommendation.recommender,
        transition_id=recommendation.recommendation_id,
        **payload,
    )


def evaluate_merge_as_link(actor: str, candidate: MergeCandidate) -> MergeDecision:
    """Evaluate exact-head merge eligibility only for the Link seat."""

    require_authority(actor, Action.EVALUATE_MERGE)
    if actor.strip().lower() != Seat.LINK:
        raise BoundaryError("only link may evaluate the merge queue")
    from .link_merge_authority import evaluate_link_merge

    return evaluate_link_merge(candidate)


def authorize_recommendation_action(
    recommendation: DispatchRecommendation,
    *,
    actor: str,
    action: Action,
    current_claim_owner: str | None,
    current_claim_revision: str | None,
    current_process: Mapping[str, object],
    used_recommendation_ids: Collection[str],
) -> None:
    """Fence Niobe action against replay and stale CardStore/process state."""

    recommendation.validate()
    require_authority(actor, action)
    if actor.strip().lower() != Seat.NIOBE:
        raise BoundaryError("only niobe may act on a recurring recommendation")
    if recommendation.recommendation_id in used_recommendation_ids:
        raise BoundaryError("recommendation replay denied")
    if recommendation.observed_claim_owner != current_claim_owner:
        raise BoundaryError("claim owner changed after observation")
    if (
        not recommendation.observed_claim_revision
        or not recommendation.observed_claim_revision.strip()
    ):
        raise BoundaryError("observed claim revision is required")
    if not current_claim_revision or not current_claim_revision.strip():
        raise BoundaryError("current claim revision is required")
    if recommendation.observed_claim_revision != current_claim_revision:
        raise BoundaryError("claim revision changed after observation")
    if dict(recommendation.observed_process) != dict(current_process):
        raise BoundaryError("process state changed after observation")


def utc_now() -> datetime:
    """Return an aware UTC timestamp for recommendation producers."""

    return datetime.now(timezone.utc)
