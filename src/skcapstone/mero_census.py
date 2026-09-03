"""Read-only recurring blocker census and typed recommendations for Mero.

Card 2516480b extends Mero from one-shot observation to a recurring, bounded
census. Mero reads CardStore lifecycle, blocker attributes, worker joins,
review joins, and SKMail signals, then emits typed append-only
recommendations. Jarvis or another explicitly fenced consumer performs any
mutation; Mero never does.

WHY A CENSUS MODULE AND NOT MORE ROTATION LOGIC. The rotation decides what to
launch and runs on every host every cycle. The census only answers "what on
the board is stuck, and what should a consumer consider doing about it", so it
lives beside the other board-repair modules (``blocker_referent``) rather than
inside the scheduler.

BOUNDED. One run examines at most ``MAX_CARDS_EXAMINED`` cards and emits at
most ``MAX_FINDINGS_PER_RUN`` recommendations, so a pathological board cannot
make the census unbounded.

TYPED, APPEND-ONLY OUTPUT. Each finding becomes one
``mero_blocker_recommendation`` event carrying the card id, the card revision,
the claim revision, the blocker generation, the source events joined, evidence
hashes, a risk class, the proposed consumer action, and stop conditions. The
payload is built with ``json.dumps`` and every line is parsed with
``json.loads`` before use; nothing is ever concatenated into JSON.

STRUCTURAL JOIN, NOT LIFECYCLE INFERENCE. A verdict is never inferred from
lifecycle state or from links alone. Verdict events, blocker events, worker
claims, review receipts, and SKMail signals are joined as separate evidence
rows, and every finding cites the exact events it joined.

DEDUPLICATION. ``recommendation_id`` is a deterministic digest of the finding
without its observation timestamp, and it is passed as the CardStore
``transition_id``, so re-running the census on an unchanged board appends
nothing. A finding re-emits only when its authoritative generation changes
(state, claim, blocker evidence, or verdict set) or when its SLA is missed.

AUTHORITY. Every emission calls ``require_authority("mero",
Action.RECOMMEND)``. Mero holds exactly OBSERVE and RECOMMEND; claim, release,
launch, stop, reassign, rotate, repair, merge, deploy, card creation and card
mutation, selector reruns, and any read of protected data are all
outside its seat. The negative tests prove each refusal.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .card_store import Card, CardStore
from .seat_boundaries import Action, BoundaryError, require_authority

__all__ = [
    "CensusFindingType",
    "CensusReport",
    "MeroBlockerCensus",
    "MAX_CARDS_EXAMINED",
    "MAX_FINDINGS_PER_RUN",
    "DEFAULT_STALE_CLAIM_SLA",
    "DEFAULT_RECOMMENDATION_SLA",
    "RECOMMENDATION_EVENT",
    "RECOMMENDATION_SCHEMA",
    "RISK_ORDER",
    "recommendation_event_to_json",
    "parse_recommendation_line",
    "run_blocker_census",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: Census bounds. Bounded input, bounded output.
MAX_CARDS_EXAMINED = 4000
MAX_FINDINGS_PER_RUN = 200

#: Default age after which an unchanged DOING claim is reported stale.
DEFAULT_STALE_CLAIM_SLA = timedelta(hours=24)

#: Default age after which an unanswered emitted recommendation is re-emitted.
DEFAULT_RECOMMENDATION_SLA = timedelta(hours=48)

#: The event action and schema of a census recommendation.
RECOMMENDATION_EVENT = "mero_blocker_recommendation"
RECOMMENDATION_SCHEMA = "skfleet.mero-blocker-recommendation/v1"

#: Deterministic findings digest under this prefix; bump to re-key findings.
_GENERATION_VERSION = "mrc1"

#: Risk classes, highest first. Findings sort by this order, then card id.
RISK_ORDER = ("high", "medium", "low", "info")

#: A verdict text whose leading token declares BLOCKED.
_BLOCKED_RE = re.compile(r"^\s*BLOCKED\b", re.IGNORECASE)

#: A PASS that only declares work ready for review. It has not cleared its own
#: independent review, so it is not a completed pass.
_PROVISIONAL_PASS_RE = re.compile(r"^PASS[_-](FOR|READY)", re.IGNORECASE)

#: Verdict tokens that contradict a block when they are the latest outcome.
_PASS_TOKENS = ("PASS", "DONE", "COMPLETE")

#: Recognised outcome keys, matching the spellings the store actually writes.
_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)

#: Event actions that carry blocker attributes on some cards.
_BLOCKER_ACTIONS = frozenset({"blocked_on", "block", "blocked", "blocked_verdict"})

#: Link keys that name a card's repair, re-review, or supersession successor.
_SUCCESSOR_KEY_RE = re.compile(
    r"(repair|rereview|re_review|reviewed_by|successor|superseded_by)", re.IGNORECASE
)

#: Referent values the BLOCKED verdict contract allows.
_BLOCKED_ON_VALUES = ("dependency", "human", "capability", "card")


class RiskClass(StrEnum):
    """Consumer risk classes for census recommendations."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CensusFindingType(StrEnum):
    """The bounded set of census findings card 2516480b requires."""

    DEAD_CLAIM = "dead_claim"
    STALE_CLAIM = "stale_claim"
    COMPLETED_DEPENDENCY = "completed_dependency"
    CONTRADICTORY_VERDICTS = "contradictory_verdicts"
    MALFORMED_BLOCKER_REFERENT = "malformed_blocker_referent"
    VOID_DEPENDENCY_EDGE = "void_dependency_edge"
    SUPERSEDED_LIVE_CARD = "superseded_live_card"
    REVIEW_IDENTITY_GAP = "review_identity_gap"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    """Parse the store's ISO-8601 stamps; None when absent or malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _norm_id(value: object) -> str:
    """Normalise a card id reference to the store's lowercase hex shape."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    return text.lower()


def _verdict_head(value: str) -> str:
    """The leading token of a verdict, uppercased.

    A PASS routinely explains what it supersedes, so it can contain the word
    BLOCKED in prose. Only the leading token is the verdict.
    """
    if _PROVISIONAL_PASS_RE.match(str(value or "").strip().upper()):
        return str(value or "").strip().split()[0].upper()
    head = str(value or "").strip()
    for sep in ("|", ";", ":", ",", "."):
        head = head.split(sep)[0]
    return head.strip().split()[0].upper() if head.strip() else ""


def _is_block(value: str) -> bool:
    return bool(_BLOCKED_RE.match(str(value or "")))


def _is_pass_token(value: str) -> bool:
    head = _verdict_head(value)
    if not head.startswith("PASS"):
        return False
    # A PASS_FOR_REVIEW has not cleared its own independent review yet, so it
    # cannot contradict or discharge a block. Only a completed pass can.
    return not _PROVISIONAL_PASS_RE.match(head)


def _canonical_digest(payload: object) -> str:
    """SHA-256 over canonical JSON of ``payload``."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _generation_key(*parts: object) -> str:
    """A short deterministic key over the authoritative inputs of a finding."""
    return _canonical_digest([_GENERATION_VERSION, *(str(p) for p in parts)])[:32]


def _event_ref(event: dict) -> dict:
    """The identifying projection of one source event."""
    return {
        "event_id": str(event.get("event_id") or ""),
        "ts": str(event.get("ts") or ""),
        "action": str(event.get("action") or ""),
        "writer": str(event.get("writer") or ""),
        "seq": event.get("seq"),
    }


def recommendation_event_to_json(event: dict) -> str:
    """Serialize one recommendation event line. Never concatenate JSON."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def parse_recommendation_line(line: str) -> dict:
    """Parse one recommendation event line, rejecting non-JSON input."""
    event = json.loads(line)
    if not isinstance(event, dict):
        raise ValueError("recommendation line is not a JSON object")
    return event


def _referent_defect(event: dict, resolve_card: Callable[[str], object]) -> str | None:
    """Why a blocker event's typed referent violates the BLOCKED contract.

    Returns None when the event carries a well-formed referent, per the four
    allowed ``blocked_on`` values and their referent shapes:
    dependency -> card:<id>, human -> approval:<what>, capability -> ac:<n>|free,
    card -> ac:<n>.
    """
    blocked = event.get("blocked_on")
    if isinstance(blocked, dict):
        value = str(blocked.get("value") or blocked.get("type") or "").strip().lower()
        referent = str(blocked.get("referent") or "").strip()
    elif isinstance(blocked, str) and blocked.strip():
        text = blocked.strip()
        match = re.search(r"\b(dependency|human|capability|card)\b", text, re.IGNORECASE)
        value = match.group(1).lower() if match else ""
        ref_match = re.search(r"referent[\"']?\s*[=:]?\s*[\"']?([^\s,;\"']+)", text, re.IGNORECASE)
        referent = ref_match.group(1).strip("\"'") if ref_match else ""
    else:
        # No blocked_on at all: the malformed-blocker census reads the
        # verdict prose before declaring the typed fields absent.
        text = str(event.get("verdict") or event.get("reason") or "")
        match = re.search(r"\b(dependency|human|capability|card)\b", text, re.IGNORECASE)
        value = match.group(1).lower() if match else ""
        ref_match = re.search(r"referent[\"']?\s*[=:]?\s*[\"']?([^\s,;\"']+)", text, re.IGNORECASE)
        referent = ref_match.group(1).strip("\"'") if ref_match else ""
        if not value and not referent:
            return "missing_or_unknown_blocked_on_value"
    if value not in _BLOCKED_ON_VALUES:
        return "missing_or_unknown_blocked_on_value"
    if not referent:
        return "missing_referent"
    if value == "dependency":
        # A dependency referent must be card:<hex id>. Do not liberalise with
        # a strip-nonhex pass: "notacard" would otherwise normalize to hex
        # "acad" and look like a plausible id.
        id_match = re.fullmatch(r"(?:card:)?([0-9a-f]{8,32})", referent, re.IGNORECASE)
        if id_match is None:
            return "dependency_referent_not_a_card_id"
        target = id_match.group(1).lower()
        if resolve_card(target) is None:
            return "dependency_referent_unresolvable"
    elif value == "human":
        if not referent:
            return "human_referent_missing"
    elif value == "capability":
        if not (re.fullmatch(r"ac:\d+", referent, re.IGNORECASE) or referent.lower() == "free"):
            return "capability_referent_not_ac_or_free"
    else:  # card
        if not re.fullmatch(r"ac:\d+", referent, re.IGNORECASE):
            return "card_referent_not_ac"
    return None


@dataclass
class CensusReport:
    """The bounded result of one census run. No mutations are recorded here."""

    census_id: str
    observed_at: str
    cards_examined: int
    cards_total: int
    truncated: bool
    findings: list[dict] = field(default_factory=list)
    suppressed_unchanged: int = 0
    suppressed_by_bound: int = 0
    selector_ready: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


class MeroBlockerCensus:
    """One bounded read-only pass over the board, plus its typed emissions.

    The census reads lifecycle, blocker attributes, worker joins, review
    joins, and SKMail signals, joins them per card, and produces findings.
    Nothing is written during the pass. Emission is a separate, explicit,
    append-only step reserved to the Mero seat.

    Worker, identity, and SKMail joins are injected as callables so the census
    never touches process state or mailboxes implicitly. Defaults observe
    nothing, which yields no worker-dependent findings.
    """

    def __init__(
        self,
        home: Path,
        *,
        now: Callable[[], datetime] | None = None,
        process_reader: Callable[[str], dict] | None = None,
        identity_reader: Callable[[str], bool] | None = None,
        skmail_reader: Callable[[str], list[dict]] | None = None,
        max_cards: int = MAX_CARDS_EXAMINED,
        max_findings: int = MAX_FINDINGS_PER_RUN,
        stale_claim_sla: timedelta = DEFAULT_STALE_CLAIM_SLA,
        recommendation_sla: timedelta = DEFAULT_RECOMMENDATION_SLA,
    ) -> None:
        self.home = Path(home)
        self._now = now or _now
        self._process_reader = process_reader or (lambda cid: {})
        self._identity_reader = identity_reader or (lambda cid: True)
        self._skmail_reader = skmail_reader or (lambda cid: [])
        self.max_cards = int(max_cards)
        self.max_findings = int(max_findings)
        self.stale_claim_sla = stale_claim_sla
        self.recommendation_sla = recommendation_sla

    # -- board reads ---------------------------------------------------------

    def _card_exists(self, cid: str) -> object:
        """Resolve a card id or 8-plus-hex reference, or None if unknown.

        Referents may cite a full id or a longer prefix; the store's ids are
        8 hex chars, so a longer citation resolves through its first 8.
        """
        store = CardStore(self.home)
        for candidate in (cid, cid[:8]):
            if not candidate:
                continue
            try:
                card = store.fold(candidate)
            except Exception:  # noqa: BLE001 - an unreadable target is not a card
                continue
            if card is not None:
                return card
        return None

    def _read_events(self, cid: str) -> list[dict]:
        """Parse every event line for a card through the store's reader."""
        store = CardStore(self.home)
        try:
            rows = store._read_events(cid)
        except Exception:  # noqa: BLE001 - unreadable evidence yields no rows
            return []
        return [row for row in rows if isinstance(row, dict)]

    def _card_facts(self, card: Card) -> dict:
        """Join every census signal for one card in a single event read.

        Verdict rows, blocker rows, claims, releases, voids, review receipts,
        dependency edits, progress, observations, and SKMail signals are kept
        as separate evidence lists. Nothing is inferred across stores here.
        """
        facts: dict = {
            "events": [],
            "claims": [],
            "releases": [],
            "verdict_rows": [],
            "blocker_rows": [],
            "void_rows": [],
            "review_rows": [],
            "dep_adds": [],
            "dep_removes": [],
            "progress_rows": [],
            "observation_rows": [],
            "successor_links": [],
        }
        events = self._read_events(card.id)
        facts["events"] = events
        for event in events:
            action = str(event.get("action") or "")
            if action == "claim":
                facts["claims"].append(event)
            elif action == "release_claim":
                facts["releases"].append(event)
            elif action == "void":
                facts["void_rows"].append(event)
            elif action in ("review_assignment_launch", "review_assignment_recommendation"):
                facts["review_rows"].append(event)
            elif action == "add_dependency":
                facts["dep_adds"].append(event)
            elif action == "remove_dependency":
                facts["dep_removes"].append(event)
            elif action in _BLOCKER_ACTIONS:
                facts["blocker_rows"].append(event)
            elif action in (
                "move",
                "describe",
                "amend_criteria",
                "set_priority",
                "priority",
                "add_label",
                "remove_label",
                "link",
            ):
                facts["progress_rows"].append(event)
            elif action == "mero_observation":
                facts["observation_rows"].append(event)
            if action in ("verdict", "evidence", "record_verdict") or _OUTCOME_KEY_RE.search(
                action
            ):
                verdict = event.get("verdict")
                if verdict is None and isinstance(event.get("outcome"), str):
                    verdict = event["outcome"]
                if isinstance(verdict, str) and verdict.strip():
                    facts["verdict_rows"].append(event)
            if action == "link" and _SUCCESSOR_KEY_RE.search(str(event.get("link_key") or "")):
                facts["successor_links"].append(event)
        facts["skmail"] = self._skmail_reader(card.id)
        facts["process"] = self._process_reader(card.id)
        facts["identity_fresh"] = bool(self._identity_reader(card.id))
        return facts

    # -- derived facts -------------------------------------------------------

    @staticmethod
    def _latest(rows: list[dict]) -> dict | None:
        """The newest row by parsed timestamp, falling back to list order."""
        best: dict | None = None
        best_key: tuple[datetime, int] | None = None
        for index, row in enumerate(rows):
            stamp = _parse_ts(row.get("ts"))
            key = (stamp or datetime.min.replace(tzinfo=timezone.utc), index)
            if best_key is None or key > best_key:
                best, best_key = row, key
        return best

    def _claim_state(self, card: Card, facts: dict) -> dict:
        """Claim liveness facts derived only from joined events."""
        claim = self._latest(facts["claims"])
        state = {
            "claim": claim,
            "claim_revision": str((claim or {}).get("claim_revision") or ""),
            "released": False,
            "progress_after_claim": False,
            "claim_age": None,
        }
        if claim is None:
            return state
        claim_ts = _parse_ts(claim.get("ts"))
        state["claim_age"] = self._now() - claim_ts if claim_ts is not None else None
        for release in facts["releases"]:
            release_ts = _parse_ts(release.get("ts"))
            if release_ts is None or claim_ts is None or release_ts >= claim_ts:
                state["released"] = True
                break
        for progress in facts["progress_rows"]:
            progress_ts = _parse_ts(progress.get("ts"))
            if progress_ts is not None and claim_ts is not None and progress_ts > claim_ts:
                state["progress_after_claim"] = True
                break
        return state

    @staticmethod
    def _outcome_rows(facts: dict) -> list[tuple[datetime, str, str]]:
        """Verdict rows as (parsed ts, verdict head, raw verdict), ordered."""
        rows: list[tuple[datetime, str, str]] = []
        for event in facts["verdict_rows"]:
            raw = str(event.get("verdict") or event.get("outcome") or "")
            stamp = _parse_ts(event.get("ts")) or datetime.min.replace(tzinfo=timezone.utc)
            rows.append((stamp, _verdict_head(raw), raw))
        rows.sort(key=lambda row: row[0])
        return rows

    def _latest_outcome(self, facts: dict) -> tuple[datetime, str, str] | None:
        rows = self._outcome_rows(facts)
        return rows[-1] if rows else None

    # -- finding construction --------------------------------------------------

    def _finding(
        self,
        card: Card,
        finding_type: CensusFindingType,
        facts: dict,
        *,
        risk: RiskClass,
        action: str,
        stop_conditions: list[str],
        source_events: list[dict],
        details: dict | None = None,
    ) -> dict:
        """Pin one finding: card, revisions, generations, sources, and action.

        The recommendation id digests the finding without its observation
        timestamp, so an unchanged board yields the identical id and the
        CardStore dedupes it by transition id.
        """
        details = dict(details or {})
        claim_state = self._claim_state(card, facts)
        claim_revision = claim_state["claim_revision"]
        status_value = str(getattr(card.status, "value", card.status))
        card_revision = _generation_key(
            card.id, card.owner or "", status_value, str(card.updated_at or "")
        )
        evidence_hashes = [_canonical_digest(_event_ref(event)) for event in source_events]
        blocker_generation = _generation_key(
            card.id,
            card.owner or "",
            status_value,
            claim_revision,
            *sorted(evidence_hashes),
        )
        generation = _generation_key(
            card.id, finding_type.value, blocker_generation, str(details.get("sla_state") or "")
        )
        payload: dict = {
            "schema": RECOMMENDATION_SCHEMA,
            "card_id": card.id,
            "finding_type": finding_type.value,
            "observed_by": "mero",
            "card_revision": card_revision,
            "claim_revision": claim_revision,
            "blocker_generation": blocker_generation,
            "generation": generation,
            "risk_class": risk.value,
            "proposed_consumer_action": action,
            "stop_conditions": [str(s) for s in stop_conditions],
            "source_events": [_event_ref(event) for event in source_events],
            "evidence_sha256": evidence_hashes,
            "details": details,
        }
        payload["observed_at"] = self._now().isoformat()
        # The recommendation id digests the SEMANTIC finding: neither the
        # observation timestamp nor card_revision participates. Emitting a
        # recommendation appends an event, which bumps the card's updated_at;
        # folding card_revision into the id would make every census run
        # mint a fresh id for an unchanged finding and defeat dedupe. Card
        # state that matters to a finding is pinned in blocker_generation
        # (owner, status, claim revision, evidence hashes) instead.
        payload["recommendation_id"] = (
            "mrc-"
            + _canonical_digest(
                {k: v for k, v in payload.items() if k not in ("observed_at", "card_revision")}
            )[:32]
        )
        return payload

    # -- detectors -----------------------------------------------------------

    def _detect_dead_claim(self, card: Card, facts: dict) -> list[dict]:
        """A claim whose worker process and identity evidence are gone."""
        if card.owner is None or facts["lifecycle_terminal"]:
            return []
        claim_state = self._claim_state(card, facts)
        claim = claim_state["claim"]
        if claim is None or claim_state["released"]:
            return []
        if claim_state["progress_after_claim"]:
            return []
        process = facts["process"]
        sessions = process.get("sessions") if isinstance(process, dict) else None
        if sessions != [] or facts["identity_fresh"]:
            return []
        age = claim_state["claim_age"]
        sla_state = "" if age is None else ("missed" if age > self.stale_claim_sla else "at_risk")
        sources = [claim, *facts["observation_rows"]]
        finding = self._finding(
            card,
            CensusFindingType.DEAD_CLAIM,
            facts,
            risk=RiskClass.HIGH,
            action="jarvis_release_claim_and_optionally_relaunch",
            stop_conditions=[
                "stop if a live worker session for this claim is now visible",
                "stop if the claim revision changed after observation",
                "stop if the card left doing or gained progress after the claim",
            ],
            source_events=sources,
            details={
                "claim_revision": claim_state["claim_revision"],
                "observed_host": str(process.get("host") or ""),
                "claim_age_hours": None if age is None else round(age.total_seconds() / 3600, 2),
                "sla_state": sla_state,
            },
        )
        return [finding]

    def _detect_stale_claim(self, card: Card, facts: dict) -> list[dict]:
        """A DOING claim older than the SLA with no progress after it."""
        if card.owner is None or facts["lifecycle_terminal"]:
            return []
        claim_state = self._claim_state(card, facts)
        claim = claim_state["claim"]
        if claim is None or claim_state["released"] or claim_state["progress_after_claim"]:
            return []
        age = claim_state["claim_age"]
        if age is None or age < self.stale_claim_sla:
            return []
        finding = self._finding(
            card,
            CensusFindingType.STALE_CLAIM,
            facts,
            risk=RiskClass.MEDIUM,
            action="jarvis_review_stale_claim_for_release_or_reassignment",
            stop_conditions=[
                "stop if progress events newer than the claim appear",
                "stop if the card left doing",
                "stop if the claim revision changed after observation",
            ],
            source_events=[claim],
            details={
                "claim_revision": claim_state["claim_revision"],
                "claim_age_hours": round(age.total_seconds() / 3600, 2),
                "sla_state": "missed" if age > 2 * self.stale_claim_sla else "at_risk",
            },
        )
        return [finding]

    def _detect_completed_dependency(
        self, card: Card, facts: dict, done_ids: set[str]
    ) -> list[dict]:
        """An open card whose declared dependencies have all completed."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        completed = [dep for dep in card.dependencies if _norm_id(dep) in done_ids]
        if not completed:
            return []
        done_set = {_norm_id(d) for d in completed}
        sources = [
            event for event in facts["dep_adds"] if _norm_id(event.get("dependency")) in done_set
        ]
        # Dependency completion is structurally true from the fold, but a
        # recommendation must cite events. Cite the card's own completion of
        # the dependency graph: the move-to-done events of the completed deps
        # joined separately below, or fall back to this card's dep edges.
        if not sources:
            sources = [
                event
                for event in facts["events"]
                if str(event.get("action")) in ("add_dependency", "claim", "verdict")
            ]
        finding = self._finding(
            card,
            CensusFindingType.COMPLETED_DEPENDENCY,
            facts,
            risk=RiskClass.LOW,
            action="consumer_reopen_card_whose_blockers_completed",
            stop_conditions=[
                "stop if any dependency is no longer done on a fresh read",
                "stop if the card is no longer open",
                "stop if the dependency list changed after observation",
            ],
            source_events=sources,
            details={"completed_dependencies": sorted(completed)},
        )
        return [finding]

    def _detect_void_dependency_edge(
        self, card: Card, facts: dict, void_ids: set[str]
    ) -> list[dict]:
        """An open card that still depends on a voided card."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        voided = [dep for dep in card.dependencies if _norm_id(dep) in void_ids]
        if not voided:
            return []
        void_set = {_norm_id(d) for d in voided}
        sources = [
            event for event in facts["dep_adds"] if _norm_id(event.get("dependency")) in void_set
        ]
        if not sources:
            sources = [
                event
                for event in facts["events"]
                if str(event.get("action")) in ("add_dependency", "claim", "verdict")
            ]
        finding = self._finding(
            card,
            CensusFindingType.VOID_DEPENDENCY_EDGE,
            facts,
            risk=RiskClass.MEDIUM,
            action="consumer_cut_or_replace_void_dependency_edge",
            stop_conditions=[
                "stop if a cited dependency is no longer void on a fresh read",
                "stop if the card's dependency list changed after observation",
            ],
            source_events=sources,
            details={"void_dependencies": sorted(voided)},
        )
        return [finding]

    def _detect_contradictory_verdicts(self, card: Card, facts: dict) -> list[dict]:
        """A block that is the latest outcome while an earlier pass exists."""
        rows = self._outcome_rows(facts)
        if not rows or not _is_block(rows[-1][2]):
            return []
        passes = [row for row in rows if _is_pass_token(row[2])]
        if not passes:
            return []
        latest_block, latest_pass = rows[-1], passes[-1]
        sources = [
            event
            for event in facts["verdict_rows"]
            if _parse_ts(event.get("ts")) in (latest_block[0], latest_pass[0])
        ]
        finding = self._finding(
            card,
            CensusFindingType.CONTRADICTORY_VERDICTS,
            facts,
            risk=RiskClass.HIGH,
            action="consumer_reconcile_contradictory_verdicts",
            stop_conditions=[
                "stop if a fresh read shows a verdict newer than the block",
                "stop if the contradicting verdicts were retracted",
            ],
            source_events=sources,
            details={
                "blocked_at": latest_block[0].isoformat(),
                "block_verdict": latest_block[2][:200],
                "passed_at": latest_pass[0].isoformat(),
                "pass_verdict": latest_pass[2][:200],
            },
        )
        return [finding]

    def _detect_malformed_blocker_referents(self, card: Card, facts: dict) -> list[dict]:
        """Blocker events whose typed referent violates the BLOCKED contract."""
        findings: list[dict] = []
        for event in facts["blocker_rows"] + [
            row for row in facts["verdict_rows"] if _is_block(str(row.get("verdict") or ""))
        ]:
            defect = _referent_defect(event, self._card_exists)
            if defect is None:
                continue
            findings.append(
                self._finding(
                    card,
                    CensusFindingType.MALFORMED_BLOCKER_REFERENT,
                    facts,
                    risk=RiskClass.LOW,
                    action="consumer_correct_blocked_on_referent_shape",
                    stop_conditions=[
                        "stop if a corrected blocker event supersedes this one",
                        "stop if the defect no longer reproduces on a fresh read",
                    ],
                    source_events=[event],
                    details={"defect": defect, "blocked_on_raw": str(event.get("blocked_on"))},
                )
            )
        return findings

    def _detect_superseded_live_card(
        self, card: Card, facts: dict, done_ids: set[str]
    ) -> list[dict]:
        """An open card that a void or a completed successor has superseded."""
        if facts["lifecycle_terminal"] or card.archived:
            return []
        successors: list[str] = []
        sources: list[dict] = []
        for event in facts["void_rows"]:
            reason = str(event.get("reason") or "")
            match = re.search(r"\b([0-9a-f]{8})\b", reason)
            if match:
                successors.append(match.group(1).lower())
            sources.append(event)
        for link in facts["successor_links"]:
            target = _norm_id(str(link.get("link_value") or ""))[:8]
            if target and target in done_ids:
                successors.append(target)
                sources.append(link)
        successors = sorted(set(successors))
        if not successors:
            return []
        finding = self._finding(
            card,
            CensusFindingType.SUPERSEDED_LIVE_CARD,
            facts,
            risk=RiskClass.MEDIUM,
            action="consumer_void_or_close_superseded_live_card",
            stop_conditions=[
                "stop if the card gained progress after the supersession",
                "stop if the named successor is no longer done or is itself void",
            ],
            source_events=sources,
            details={"successors": successors},
        )
        return [finding]

    def _detect_review_identity_gap(self, card: Card, facts: dict) -> list[dict]:
        """Review receipts whose seat identities violate the boundary.

        A recommendation must be written by link, a launch by jarvis, and the
        assigned reviewer must be distinct from the card's workers and from
        link. A review-column card with no receipt at all is also a gap.
        """
        status_value = str(getattr(card.status, "value", card.status))
        is_review_card = "review" in {str(label).strip().lower() for label in card.labels}
        if not is_review_card:
            return []
        workers = {
            str(event.get("owner") or "").strip().lower()
            for event in facts["claims"]
            if event.get("owner")
        }
        gaps: list[dict] = []
        sources: list[dict] = []
        for event in facts["review_rows"]:
            writer = str(event.get("writer") or "").strip().lower()
            action = str(event.get("action") or "")
            if action == "review_assignment_recommendation" and writer != "link":
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "recommender_not_link"}
                )
                sources.append(event)
            if action == "review_assignment_launch" and writer != "jarvis":
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "launcher_not_jarvis"}
                )
                sources.append(event)
            reviewer = str(event.get("reviewer") or "").strip().lower()
            if reviewer and (reviewer == "link" or reviewer in workers):
                gaps.append(
                    {"receipt": event.get("event_id", ""), "defect": "reviewer_not_distinct"}
                )
                sources.append(event)
        if status_value == "review" and not facts["review_rows"]:
            gaps.append({"receipt": "", "defect": "review_without_assignment_receipt"})
        if not gaps:
            return []
        finding = self._finding(
            card,
            CensusFindingType.REVIEW_IDENTITY_GAP,
            facts,
            risk=RiskClass.MEDIUM,
            action="jarvis_audit_review_assignment_identities",
            stop_conditions=[
                "stop if corrected receipts supersede every defect",
                "stop if the card left the review column",
            ],
            source_events=sources,
            details={"gaps": gaps},
        )
        return [finding]

    # -- the run -------------------------------------------------------------

    def _due_findings(self, findings: list[dict]) -> tuple[list[dict], int]:
        """Keep new findings and SLA-missed ones; drop unchanged duplicates.

        A finding is new when no recommendation with its id exists for the
        card. It re-emits when the stored generation differs (a new
        authoritative generation) or when its age exceeds the recommendation
        SLA. Everything else is an unchanged duplicate.
        """
        store = CardStore(self.home)
        prior: dict[str, dict] = {}
        cache: dict[str, dict[str, dict]] = {}
        due: list[dict] = []
        unchanged = 0
        for finding in findings:
            cid = finding["card_id"]
            if cid not in cache:
                rows: dict[str, dict] = {}
                try:
                    events = store._read_events(cid)
                except Exception:  # noqa: BLE001 - no readable history means new
                    events = []
                for event in events:
                    if isinstance(event, dict) and event.get("action") == RECOMMENDATION_EVENT:
                        rid = str(event.get("recommendation_id") or "")
                        prev = rows.get(rid)
                        if prev is None or str(event.get("ts") or "") >= str(prev.get("ts") or ""):
                            rows[rid] = event
                cache[cid] = rows
            prior = cache[cid]
            stored = prior.get(finding["recommendation_id"])
            if stored is None:
                due.append(finding)
                continue
            if str(stored.get("generation") or "") != finding["generation"]:
                due.append(finding)
                continue
            stamp = _parse_ts(stored.get("ts") or stored.get("observed_at"))
            if stamp is None or self._now() - stamp >= self.recommendation_sla:
                due.append(finding)
                continue
            unchanged += 1
        return due, unchanged

    def run(self) -> CensusReport:
        """One bounded census pass. Reads everything, writes nothing."""
        started = self._now()
        store = CardStore(self.home)
        all_ids = store.list_card_ids()
        bounded_ids = all_ids[: self.max_cards]
        done_ids: set[str] = set()
        void_ids: set[str] = set()
        cards: list[Card] = []
        for cid in bounded_ids:
            try:
                card = store.fold(cid)
            except Exception:  # noqa: BLE001 - unreadable cards are skipped, not faked
                continue
            if card is None or card.meta.get("unreadable"):
                continue
            cards.append(card)
            status_value = str(getattr(card.status, "value", card.status))
            if status_value == "done" and not card.archived:
                done_ids.add(card.id)
            # A void event is the store's structural supersession marker; the
            # fold turns it into archived + a voided meta flag. Collect every
            # voided card id, of any status, for void-edge detection.
            if card.archived or card.meta.get("voided"):
                for event in self._read_events(cid):
                    if event.get("action") == "void":
                        void_ids.add(cid)
                        break
        findings: list[dict] = []
        selector_ready = {"ready": 0, "blocked": 0, "total_open": 0}
        for card in cards:
            facts = self._card_facts(card)
            status_value = str(getattr(card.status, "value", card.status))
            # Voided cards fold to archived with a voided marker; both shapes
            # are terminal for this census.
            facts["lifecycle_terminal"] = (
                status_value == "done" or card.archived or bool(card.meta.get("voided"))
            )
            if not facts["lifecycle_terminal"]:
                selector_ready["total_open"] += 1
                outcome = self._latest_outcome(facts)
                if outcome is not None and _is_block(outcome[2]):
                    selector_ready["blocked"] += 1
                else:
                    selector_ready["ready"] += 1
            findings.extend(self._detect_dead_claim(card, facts))
            findings.extend(self._detect_stale_claim(card, facts))
            findings.extend(self._detect_completed_dependency(card, facts, done_ids))
            findings.extend(self._detect_void_dependency_edge(card, facts, void_ids))
            findings.extend(self._detect_contradictory_verdicts(card, facts))
            findings.extend(self._detect_malformed_blocker_referents(card, facts))
            findings.extend(self._detect_superseded_live_card(card, facts, done_ids))
            findings.extend(self._detect_review_identity_gap(card, facts))
        due, unchanged = self._due_findings(findings)
        risk_index = {name: index for index, name in enumerate(RISK_ORDER)}
        due.sort(
            key=lambda f: (risk_index.get(f["risk_class"], 99), f["card_id"], f["finding_type"])
        )
        suppressed_by_bound = max(0, len(due) - self.max_findings)
        counts: dict[str, int] = {}
        for finding in due[: self.max_findings]:
            counts[finding["finding_type"]] = counts.get(finding["finding_type"], 0) + 1
        return CensusReport(
            census_id="mrc-"
            + _generation_key(started.isoformat(), self.home, len(cards), len(findings)),
            observed_at=started.isoformat(),
            cards_examined=len(cards),
            cards_total=len(all_ids),
            truncated=len(all_ids) > len(bounded_ids),
            findings=due[: self.max_findings],
            suppressed_unchanged=unchanged,
            suppressed_by_bound=suppressed_by_bound,
            selector_ready=selector_ready,
            counts=counts,
        )

    # -- emission ------------------------------------------------------------

    def emit(self, report: CensusReport, *, actor: str = "mero") -> list[dict]:
        """Append the report's findings as typed events. Mero seat only.

        Every payload is serialized with ``json.dumps`` and appended through
        ``CardStore.append_event``, which dedupes by the recommendation id.
        Nothing here claims, releases, launches, stops, merges, deploys,
        creates, mutates lifecycle state, reruns the selector, or touches
        protected data it has no authority over.
        """
        require_authority(actor, Action.RECOMMEND)
        if actor.strip().lower() != "mero":
            raise BoundaryError("only mero may emit census recommendations")
        store = CardStore(self.home)
        emitted: list[dict] = []
        for finding in report.findings:
            # The timestamp is deliberately NOT part of the recommendation id
            # digest: see _finding. Emitting the same finding twice produces
            # the same transition id, and the store returns the durable event.
            payload = dict(finding)
            line = recommendation_event_to_json(payload)
            parsed = parse_recommendation_line(line)
            card_id = str(parsed.pop("card_id"))
            event = store.append_event(
                card_id,
                RECOMMENDATION_EVENT,
                "mero",
                transition_id=str(parsed["recommendation_id"]),
                **parsed,
            )
            emitted.append(event)
        return emitted


def run_blocker_census(
    home: Path,
    *,
    emit: bool = False,
    actor: str = "mero",
    **kwargs: object,
) -> CensusReport:
    """Run one census and optionally append its recommendations.

    With ``emit=False`` (the default) the census is a pure read: no event, no
    lifecycle change, no mail, no mutation of any kind.
    """
    census = MeroBlockerCensus(home, **kwargs)  # type: ignore[arg-type]
    report = census.run()
    if emit:
        census.emit(report, actor=actor)
    return report
