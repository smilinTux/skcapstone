"""Find cards whose exact card blockers now fold to DONE.

The sweep is intentionally strict. Only a latest outcome beginning with
``BLOCKED`` and containing one exact ``blocked_on=card`` token plus one or
more distinct ``referent=card:<8 lowercase hex>`` tokens can qualify.
Everything ambiguous fails closed.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from skcoord.card import CardEvent, CardEventLog, Column
from skcoord.card_store import CardStore, card_mutation_lock

from .coord_amendments import is_voided

LABEL = "blocker-now-done"
VERDICT_MARKER_KEY = "blocker_return_marker"
STALE_LABEL = "successor-passed"

_OUTCOME_KEY_RE = re.compile(
    r"(verdict|outcome|result|disposition|review_decision)", re.IGNORECASE
)
_BLOCKED_RE = re.compile(r"^\s*BLOCKED\b")
_BLOCKED_ON_MARKER_RE = re.compile(r"\bblocked_on\b")
_BLOCKED_ON_TOKEN_RE = re.compile(
    r"\bblocked_on=(dependency|card|human|capability)(?=$|[\s,.;|}\]])"
)
_REFERENT_MARKER_RE = re.compile(r"\breferent\b")
_REFERENT_TOKEN_RE = re.compile(r"\breferent=card:([0-9a-f]{8})(?=$|[\s,.;|}\]])")
_CRITERION_RE = re.compile(r"\bac\s*[:=]\s*\d+\b", re.IGNORECASE)

#: Link keys naming the card that repairs or independently re-reviews THIS card.
#: When one passes, the block that named it is answered, but nothing on the
#: board propagates that back, so the parent keeps reading BLOCKED.
_SUCCESSOR_KEY_RE = re.compile(
    r"(repair_card|repair|rereview|re_review|reviewed_by|successor)", re.IGNORECASE
)

#: A verdict declaring work only READY for review. Not a pass, and it must never
#: discharge a block: 6dd21df9 recorded PASS_FOR_INDEPENDENT_REVIEW and its own
#: review, 335c91c6, then BLOCKED. Treating the first as a pass would have
#: cleared ae993252, the card gating two production approval gates.
_PROVISIONAL_PASS_RE = re.compile(r"^PASS[_-](FOR|READY)", re.IGNORECASE)

#: Separators that terminate the leading token of a verdict. A PASS routinely
#: explains what it supersedes and so contains BLOCKED in prose; substring
#: matching reports real passes as refusals.
_HEAD_SEPARATORS = ("|", ";", ":", ",", ".")


@dataclass(frozen=True)
class Verdict:
    """One latest outcome with a stable source identity."""

    card_id: str
    ts: str
    writer: str
    seq: int
    occurrence: int
    action: str
    key: str
    value: str

    @property
    def identity(self) -> str:
        """Return a non-secret digest identifying these exact verdict bytes."""
        raw = json.dumps(
            [
                self.card_id,
                self.ts,
                self.writer,
                self.seq,
                self.occurrence,
                self.action,
                self.key,
                self.value,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    """A target card and the exact verdict that made it returnable."""

    card_id: str
    verdict: Verdict
    referents: tuple[str, ...]


@dataclass
class SweepReport:
    """Read-only classification of latest card outcomes."""

    candidates: list[Candidate] = field(default_factory=list)
    held: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StaleBlock:
    """A block contradicted by a later pass on the card it named."""

    card_id: str
    verdict: Verdict
    link: str
    successor: str
    passed_at: str


@dataclass(frozen=True)
class ApplyReceipt:
    """Per-card result retained for audit and failure reporting."""

    card_id: str
    verdict_id: str
    state: str
    detail: str
    returncode: int = 0


def _ordered_events(home: Path) -> list[CardEvent]:
    """Read overlay events in the same order used by the canonical fold."""
    return sorted(CardEventLog(home).read_all(), key=lambda e: (e.ts, e.writer, e.seq))


def _latest_outcomes(home: Path) -> dict[str, Verdict]:
    """Return the latest outcome event for each card."""
    latest: dict[str, Verdict] = {}
    occurrences: dict[tuple[str, str, str, int, str, str, str], int] = {}
    for event in _ordered_events(home):
        key = str(event.link_key or "")
        if event.action != "link" or not _OUTCOME_KEY_RE.search(key):
            continue
        value = str(event.link_value or "")
        event_key = (
            event.card_id,
            event.ts,
            event.writer,
            event.seq,
            event.action,
            key,
            value,
        )
        occurrence = occurrences.get(event_key, 0) + 1
        occurrences[event_key] = occurrence
        latest[event.card_id] = Verdict(
            card_id=event.card_id,
            ts=event.ts,
            writer=event.writer,
            seq=event.seq,
            occurrence=occurrence,
            action=event.action,
            key=key,
            value=value,
        )
    return latest


def exact_card_referents(value: str) -> tuple[tuple[str, ...], str | None]:
    """Parse the only verdict shape safe enough for automatic return."""
    text = str(value or "")
    if not _BLOCKED_RE.match(text):
        return (), "latest outcome is not exact BLOCKED"

    categories = list(_BLOCKED_ON_TOKEN_RE.finditer(text))
    if len(categories) != 1 or categories[0].group(1) != "card":
        return (), "blocked_on must contain exactly one card category"
    if len(_BLOCKED_ON_MARKER_RE.findall(text)) != 1:
        return (), "blocked_on contains malformed or duplicate markers"
    if _CRITERION_RE.search(text):
        return (), "acceptance-criterion referents are not cards"

    matches = list(_REFERENT_TOKEN_RE.finditer(text))
    if not matches:
        return (), "no exact lowercase card referent"
    if len(_REFERENT_MARKER_RE.findall(text)) != len(matches):
        return (), "referent contains bare, malformed, or overlong tokens"
    referents = tuple(match.group(1) for match in matches)
    if len(set(referents)) != len(referents):
        return (), "duplicate card referents are ambiguous"
    return referents, None


def _is_superseded(labels: list[str]) -> bool:
    """Return whether folded labels mark a card as superseded."""
    lowered = {label.lower() for label in labels}
    return "superseded" in lowered or any(label.startswith("superseded-") for label in lowered)


def _card_state(home: Path, store: CardStore, card_id: str) -> str:
    """Classify one card through its current canonical fold."""
    try:
        card = store.fold(card_id)
        if card is None:
            return "missing"
        if is_voided(home, card_id):
            return "voided"
        if _is_superseded(card.labels):
            return "superseded"
        if card.archived:
            return "archived"
        if card.status != Column.DONE:
            return "not-DONE"
        return "DONE"
    except (OSError, RuntimeError, ValueError):
        return "unreadable"


def _returned_verdicts(home: Path, label: str) -> dict[str, set[str]]:
    """Return exact verdict identities already labelled successfully."""
    returned: dict[str, set[str]] = {}
    for event in _ordered_events(home):
        if (
            event.action == "add_label"
            and event.label == label
            and event.link_key == VERDICT_MARKER_KEY
            and event.link_value
        ):
            returned.setdefault(event.card_id, set()).add(event.link_value)
    return returned


def find_returnable(
    home: Path, *, label: str = LABEL, card_ids: set[str] | None = None
) -> SweepReport:
    """Classify latest blocked verdicts without mutating the board."""
    home = Path(home).expanduser()
    store = CardStore(home)
    returned = _returned_verdicts(home, label)
    report = SweepReport()

    for card_id, verdict in sorted(_latest_outcomes(home).items()):
        if card_ids is not None and card_id not in card_ids:
            continue
        if not _BLOCKED_RE.match(verdict.value):
            continue
        target_state = _card_state(home, store, card_id)
        if target_state == "DONE" or target_state in {
            "missing",
            "voided",
            "superseded",
            "archived",
            "unreadable",
        }:
            report.held[card_id] = f"target-{target_state}"
            continue
        if verdict.identity in returned.get(card_id, set()):
            report.held[card_id] = "already-returned-for-verdict"
            continue
        referents, error = exact_card_referents(verdict.value)
        if error:
            report.held[card_id] = error
            continue
        states = [(referent, _card_state(home, store, referent)) for referent in referents]
        failures = [f"{referent}:{state}" for referent, state in states if state != "DONE"]
        if failures:
            report.held[card_id] = "referent-not-DONE " + ",".join(failures)
            continue
        report.candidates.append(Candidate(card_id, verdict, referents))

    return report


def verdict_head(value: str) -> str:
    """The leading token of a verdict, uppercased."""
    head = str(value or "").strip()
    for separator in _HEAD_SEPARATORS:
        head = head.split(separator)[0]
    head = head.strip()
    return head.split()[0].upper() if head else ""


def is_discharging_pass(value: str) -> bool:
    """True only for a completed PASS, never one still awaiting its own review."""
    head = verdict_head(value)
    if not head.startswith("PASS"):
        return False
    return not _PROVISIONAL_PASS_RE.match(head)


def _successor_links(home: Path) -> dict[str, list[tuple[str, str]]]:
    """Cards each card names as its repair or re-review: {card: [(key, target)]}."""
    links: dict[str, list[tuple[str, str]]] = {}
    for event in _ordered_events(home):
        if event.action != "link":
            continue
        key = str(event.link_key or "")
        if not _SUCCESSOR_KEY_RE.search(key):
            continue
        target = str(event.link_value or "").strip()[:8]
        if event.card_id and len(target) == 8 and target != event.card_id:
            links.setdefault(event.card_id, []).append((key, target))
    return links


def find_stale_blocks(home: Path, *, label: str = STALE_LABEL) -> list[StaleBlock]:
    """Cards reading BLOCKED whose own named successor has since PASSED.

    The successor's PASS must come AFTER the block. An earlier pass answered
    some previous refusal, and clearing a live block on it would be wrong.

    Deliberately does NOT skip closed cards, which is where this differs from
    find_returnable. A stale verdict does its damage through whoever READS it,
    not through the card's own column: 2c35d28b folded to DONE and still held
    four approval gates shut for five days, because the gates read its verdict
    and saw BLOCKED. Filtering closed cards would hide the worst instance.

    MEASURED 2026-08-28. Thirteen cards were in this state, most repaired within
    fifteen minutes of the block that named the repair.
    """
    home = Path(home).expanduser()
    outcomes = _latest_outcomes(home)
    links = _successor_links(home)
    already = _returned_verdicts(home, label)
    stale: list[StaleBlock] = []
    for card_id, verdict in sorted(outcomes.items()):
        if not _BLOCKED_RE.match(verdict.value):
            continue
        if verdict.identity in already.get(card_id, set()):
            continue
        for key, target in links.get(card_id, []):
            hit = outcomes.get(target)
            if hit is None or not is_discharging_pass(hit.value):
                continue
            if hit.ts <= verdict.ts:
                continue
            stale.append(StaleBlock(card_id, verdict, key, target, hit.ts))
            break
    return stale


def _append_label(home: Path, candidate: Candidate, label: str, agent: str) -> None:
    """Append the fixed label and exact verdict marker as one durable event."""
    CardEventLog(home).append(
        CardEvent(
            card_id=candidate.card_id,
            action="add_label",
            label=label,
            writer=agent,
            link_key=VERDICT_MARKER_KEY,
            link_value=candidate.verdict.identity,
        )
    )


def apply_candidate(
    home: Path,
    candidate: Candidate,
    *,
    agent: str,
    label: str = LABEL,
    writer: Callable[[Path, Candidate, str, str], None] | None = None,
) -> ApplyReceipt:
    """Label one candidate once while holding the supported card lock."""
    home = Path(home).expanduser()
    writer = writer or _append_label
    try:
        with ExitStack() as locks:
            for card_id in sorted({candidate.card_id, *candidate.referents}):
                locks.enter_context(card_mutation_lock(home, card_id))
            current = find_returnable(home, label=label, card_ids={candidate.card_id})
            fresh = next(
                (
                    item
                    for item in current.candidates
                    if item.card_id == candidate.card_id
                    and item.verdict.identity == candidate.verdict.identity
                ),
                None,
            )
            if fresh is None:
                reason = current.held.get(candidate.card_id, "verdict changed")
                return ApplyReceipt(
                    candidate.card_id,
                    candidate.verdict.identity,
                    "skipped",
                    reason,
                )

            try:
                writer(home, candidate, label, agent)
            except Exception as exc:  # noqa: BLE001 - preserve every writer failure
                if candidate.verdict.identity in _returned_verdicts(home, label).get(
                    candidate.card_id, set()
                ):
                    return ApplyReceipt(
                        candidate.card_id,
                        candidate.verdict.identity,
                        "labelled",
                        f"durable label observed after writer error: {exc}",
                    )
                return ApplyReceipt(
                    candidate.card_id,
                    candidate.verdict.identity,
                    "failed",
                    str(exc) or "label writer failed without detail",
                    1,
                )
            after = find_returnable(home, label=label, card_ids={candidate.card_id})
            if after.held.get(candidate.card_id) != "already-returned-for-verdict":
                return ApplyReceipt(
                    candidate.card_id,
                    candidate.verdict.identity,
                    "failed",
                    "label writer returned success but no durable marker was observed",
                    1,
                )
            return ApplyReceipt(
                candidate.card_id,
                candidate.verdict.identity,
                "labelled",
                "durable label and exact verdict marker observed",
            )
    except Exception as exc:  # noqa: BLE001 - one card must not hide later results
        return ApplyReceipt(
            candidate.card_id,
            candidate.verdict.identity,
            "failed",
            str(exc),
            1,
        )


def apply_candidates(
    home: Path,
    candidates: list[Candidate],
    *,
    agent: str,
    label: str = LABEL,
    writer: Callable[[Path, Candidate, str, str], None] | None = None,
) -> list[ApplyReceipt]:
    """Apply every candidate and retain every per-card result."""
    return [
        apply_candidate(home, item, agent=agent, label=label, writer=writer) for item in candidates
    ]
