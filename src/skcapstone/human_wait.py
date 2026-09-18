"""Cards that can only move when a person moves them.

Some work cannot be finished by any agent. It needs the operator: an admin
console nobody else can log into, a vendor UI, a grant of access, a product
decision. A worker that hits one of those is already told what to record, and
the vocabulary for it already exists twice over:

    BLOCKED blocked_on=human referent=approval:perform-tailscale-admin-delete-chiap06

    label human-gate / "[HUMAN]" in the title

MEASURED ON THE LIVE BOARD, 2026-08-28 to 2026-08-31. Saying so achieved
nothing, for two separate reasons.

FIRST, NOTHING ENFORCED IT AT THE CLAIM PATH. On card 83e498b6 ("Remove retired
chiap06 Windows device record", which needs Chef's Tailscale admin console and
carries zero acceptance criteria) a worker wrote exactly the verdict above. The
refill waves then launched named seats that went straight through
``Board.claim_task``, which has no human gate at all, and the relaunched workers
overwrote that verdict with a retryable ``blocked_on=capability``. The card
accumulated 58 claims, 47 of them one seat re-claiming every ten minutes. The
label gate is real but lives only in the rotate pool's SELECTION, so any caller
that claims a card by id walks around it.

That is why a later outcome recorded by an AGENT does not discharge a human
hold here. Only a person can. If any writer could clear it, 83e498b6 would clear
itself on the first relaunch, which is precisely what happened.

SECOND, NOTHING SHOWED THE QUEUE TO ANYONE. Sixteen open cards sat at latest
outcome ``BLOCKED blocked_on=human`` and were surfaced to nobody: not to the
pool, which correctly refused to dispatch them, and not to the operator, who
was the only one who could move them. A card held for a person, and never shown
to that person, is just a card that stopped.

NO NEW VOCABULARY. This adds no label, no field, and no store. It reads the
verdicts workers already write through the parsers that already read them
(:mod:`skcapstone.blocked_verdict`, :func:`review_admission.parse_blocked_on_link`),
it refuses at the claim entrypoints beside the governed-review gate that already
refuses there, and it files the queue into the unified GTD as waiting-for items
through the existing gtd-ingest port. Releasing a hold uses the discharge
signals the fleet rotation already honours in ``_human_resolution_epoch``: a
human-authored approval or void, or the removal of the gate label.
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .blocked_verdict import blocked_on_category, blocked_on_referent, is_blocked_verdict
from .review_admission import parse_blocked_on_link

#: Writers whose events can discharge a hold held for a person. Same set the
#: fleet rotation's ``_human_resolution_epoch`` authorizes, so a discharge that
#: releases a card here releases it there too.
_HUMAN_ACTORS = ("chef", "human")
_HUMAN_RECORDER = "human-decision-recorder"

#: The label gate. ``human-gate`` is the spelling ``coord create`` documents and
#: ``coord_eligibility``/``skfleet-rotate`` already exclude from the pool.
#: ``[HUMAN]`` in a title is the older spelling, still live on the board and
#: unremovable (titles are immutable), so it is honoured and never written.
_HUMAN_GATE_LABELS = frozenset({"human-gate"})
_HUMAN_TITLE_MARK = "[HUMAN]"

#: The typed link that carries a blocker without a verdict around it.
_BLOCKED_ON_KEY = "blocked_on"

#: A human-authored discharge. Either the link KEY names the decision
#: (``human_approval``, ``human_void``) or its VALUE states one outright.
#: Mirrors the ``direct`` / ``gate_decision`` split in ``_human_resolution_epoch``.
_RESOLUTION_KEY_RE = re.compile(r"human.*(approval|void)|(approval|void).*human", re.IGNORECASE)
_RESOLUTION_VALUE_RE = re.compile(r"\b(APPROVED?|VOID(?:ED)?|GRANTED)\b")
_NO_APPROVAL_RE = re.compile(r"no[_\s-]?approval", re.IGNORECASE)

#: Tokens in an approval slug that say what to DO rather than WHERE to do it.
#: Only a display hint: every caller is handed the referent verbatim as well,
#: so a miss here costs a column, never an action.
_ACTION_TOKENS = frozenset(
    {
        "a",
        "access",
        "accept",
        "add",
        "admin",
        "an",
        "approval",
        "approve",
        "approved",
        "authorise",
        "authorize",
        "confirm",
        "console",
        "create",
        "decide",
        "decision",
        "delete",
        "disable",
        "do",
        "enable",
        "for",
        "grant",
        "human",
        "manual",
        "of",
        "perform",
        "provide",
        "remove",
        "review",
        "rotate",
        "sign",
        "supply",
        "the",
        "to",
        "ui",
        "update",
        "void",
    }
)
_HEXISH_RE = re.compile(r"^[0-9a-f]{8,}$|^\d+$", re.IGNORECASE)
_SLUG_SPLIT_RE = re.compile(r"[:/_.\-\s]+")

#: Terminal states. A done or voided card is not waiting on anybody.
_TERMINAL_STATUSES = frozenset({"done"})


def _is_human_actor(name: object) -> bool:
    """True when this writer is the operator rather than an agent."""
    actor = str(name or "").strip().lower()
    return actor in _HUMAN_ACTORS or _HUMAN_RECORDER in actor


def needed_system(referent: str) -> str:
    """Best-effort name of the system the person has to go to.

    ``approval:perform-tailscale-admin-delete-chiap06`` -> ``tailscale``.

    A display hint and nothing more. The referent is reported verbatim
    alongside it, so this being wrong loses a column and never an instruction.
    Deliberately not a stored field: inventing one would add vocabulary, and
    this estate already carries eight fully built mechanisms that nothing calls.
    """
    tokens = [token for token in _SLUG_SPLIT_RE.split(str(referent or "")) if token]
    for token in tokens[1:] if len(tokens) > 1 else tokens:
        lowered = token.lower()
        if lowered in _ACTION_TOKENS or _HEXISH_RE.match(lowered):
            continue
        return lowered
    return ""


@dataclass(frozen=True)
class HumanHold:
    """One card's live hold on a person, and what would discharge it."""

    card_id: str
    referent: str
    since: str
    source: str  # "verdict" | "blocked_on" | "label"


@dataclass(frozen=True)
class HumanWait:
    """One row of the waiting-on-human queue."""

    card_id: str
    title: str
    needs: str
    system: str
    since: str
    waited_hours: float
    unblocks: tuple[str, ...]
    source: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping of this row."""
        return {
            "card_id": self.card_id,
            "title": self.title,
            "needs": self.needs,
            "system": self.system,
            "since": self.since,
            "waited_hours": self.waited_hours,
            "unblocks": list(self.unblocks),
            "source": self.source,
        }


def _card_events(store: Any, card_id: str) -> list[dict]:
    """The card's events in the same union and order ``fold`` applies.

    ``fold`` merges the card's own logs with the sanctioned legacy overlay
    (``coordination/card_events``), which is where ``coord link`` writes. Reading
    only one side is the two-store split that has caught this codebase before,
    so this reads both and sorts them the way the fold does.
    """
    events = [*store._read_events(card_id), *store._legacy_events(card_id)]
    events.sort(
        key=lambda event: (
            str(event.get("ts") or ""),
            str(event.get("writer") or ""),
            event.get("seq") or 0,
        )
    )
    return events


def _discharges(event: dict) -> bool:
    """True when this event is a person discharging a hold."""
    if not _is_human_actor(event.get("writer")):
        return False
    action = str(event.get("action") or "")
    if action in {"void", "reopen"}:
        return True
    if action != "link":
        return False
    key = str(event.get("link_key") or "")
    value = str(event.get("link_value") or "")
    if _NO_APPROVAL_RE.search(f"{key} {value}"):
        return False
    return bool(_RESOLUTION_KEY_RE.search(key) or _RESOLUTION_VALUE_RE.search(value))


def hold_from_events(card_id: str, events: Iterable[dict]) -> HumanHold | None:
    """Fold an event stream into the card's live hold on a person, if any.

    A hold opens when a worker records ``blocked_on=human``, in a verdict or in
    the typed ``blocked_on`` link. It closes ONLY on a human-authored discharge.

    That asymmetry is the whole mechanism. On 83e498b6 the hold was opened
    correctly and then overwritten by the very workers the hold was meant to
    stop dispatching, because a later verdict from any writer replaced it. Here
    a non-human writer can no longer clear a hold that is waiting on a person.
    """
    hold: HumanHold | None = None
    for event in events:
        action = str(event.get("action") or "")
        if _discharges(event):
            hold = None
            continue
        if action != "link":
            continue
        key = str(event.get("link_key") or "")
        value = str(event.get("link_value") or "")
        stamp = str(event.get("ts") or "")
        if is_blocked_verdict(key, value):
            if blocked_on_category(value) == "human":
                hold = HumanHold(card_id, blocked_on_referent(value) or "", stamp, "verdict")
        elif key.strip().lower() == _BLOCKED_ON_KEY:
            parsed = parse_blocked_on_link(value)
            if parsed is not None and parsed[0] == "human":
                hold = HumanHold(card_id, parsed[1] or "", stamp, _BLOCKED_ON_KEY)
    return hold


def label_gate(card: Any) -> bool:
    """True when the card carries the board's existing human gate marking."""
    labels = {str(label).strip().lower().replace("_", "-") for label in (card.labels or ())}
    if labels & _HUMAN_GATE_LABELS:
        return True
    return _HUMAN_TITLE_MARK in str(card.title or "").upper()


def _age_hours(since: str, now: datetime) -> float:
    """Hours between an ISO stamp and ``now``; an unreadable stamp is 0.0."""
    try:
        stamp = datetime.fromisoformat(str(since))
    except (TypeError, ValueError):
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return round(max((now - stamp).total_seconds(), 0.0) / 3600.0, 1)


def waiting_on_human(home: Path, now: datetime | None = None) -> tuple[HumanWait, ...]:
    """The queue of open cards that only the operator can move, oldest first.

    Includes cards held by a ``blocked_on=human`` refusal and cards carrying the
    board's ``human-gate`` marking, because a person has to act on both and the
    operator has one queue, not two.
    """
    from .card_store import CardStore

    root = Path(home).expanduser()
    store = CardStore(root)
    cards = store.list_cards(degrade_unreadable=True)
    open_cards = [
        card
        for card in cards
        if card.status.value not in _TERMINAL_STATUSES and not card.meta.get("voided")
    ]
    dependents: dict[str, list[str]] = {}
    for card in open_cards:
        for dependency in card.dependencies or ():
            dependents.setdefault(str(dependency).lower(), []).append(card.id)

    moment = now or datetime.now(timezone.utc)
    rows: list[HumanWait] = []
    for card in open_cards:
        hold = hold_from_events(card.id, _card_events(store, card.id))
        if hold is None and label_gate(card):
            hold = HumanHold(card.id, "", str(card.created_at or ""), "label")
        if hold is None:
            continue
        rows.append(
            HumanWait(
                card_id=card.id,
                title=str(card.title or ""),
                needs=hold.referent,
                system=needed_system(hold.referent),
                since=hold.since,
                waited_hours=_age_hours(hold.since, moment),
                unblocks=tuple(sorted(dependents.get(card.id.lower(), ()))),
                source=hold.source,
            )
        )
    return tuple(sorted(rows, key=lambda row: (row.since, row.card_id)))


def assert_human_claim(home: Path, card_id: str, agent: str) -> None:
    """Fail closed when a claim would dispatch work that is waiting on a person.

    Modelled on :func:`review_admission.assert_governed_review_claim`, and
    called from the same two entrypoints, because the hole is the same one: the
    gate exists in the pool's selection and not on the claim itself, so any
    caller that names a card id walks straight past it.

    The operator is exempt. A hold held for Chef must not stop Chef.
    """
    if _is_human_actor(agent):
        return
    from .card_store import CardStore

    root = Path(home).expanduser()
    store = CardStore(root)
    card = store.fold(card_id)
    if card is None or card.status.value in _TERMINAL_STATUSES:
        return
    reasons: list[str] = []
    if label_gate(card):
        reasons.append("human_gate")
    hold = hold_from_events(card_id, _card_events(store, card_id))
    if hold is not None:
        reasons.append(f"blocked_on_human={hold.referent or 'unnamed'}")
    if reasons:
        raise ValueError("human claim denied: " + ", ".join(dict.fromkeys(reasons)))


# ---------------------------------------------------------------------------
# The GTD surface. The operator's own convention is that the unified GTD is
# where waiting-fors live, so that is where this files, through the existing
# gtd-ingest port. Not a dashboard, not a push, not a second list.
# ---------------------------------------------------------------------------

#: The (source, source_ref) pair is the idempotency key ``upsert`` reconciles
#: on, so re-running the sync patches the row it already wrote and reports
#: ``unchanged`` without a write.
GTD_SOURCE = "coord-human"


@contextmanager
def _gtd_store_at(home: Path) -> Iterator[None]:
    """Point the gtd-ingest sink at the store belonging to ``home``.

    ``skos.gtd_ingest.gtd_dir`` honours ``SK_GTD_DIR`` above every other
    resolver. The queue is derived from ``home``, so its waiting-fors belong in
    that home's GTD store and not in whichever one the ambient environment
    happens to name.
    """
    previous = os.environ.get("SK_GTD_DIR")
    os.environ["SK_GTD_DIR"] = str(Path(home).expanduser() / "coordination" / "gtd")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SK_GTD_DIR", None)
        else:
            os.environ["SK_GTD_DIR"] = previous


def gtd_text(row: HumanWait) -> str:
    """The waiting-for line an operator reads without opening the board."""
    need = row.needs or "a decision"
    where = f" ({row.system})" if row.system else ""
    unblocks = f" Unblocks {', '.join(row.unblocks)}." if row.unblocks else ""
    return f"[{row.card_id}] {row.title} - needs {need}{where}.{unblocks}"


def sync_gtd(home: Path, rows: Sequence[HumanWait]) -> list[tuple[str, str, str]]:
    """Upsert each queue row as a GTD waiting-for item.

    Returns:
        ``(card_id, item_id, action)`` per row, with ``action`` one of
        ``created`` / ``updated`` / ``unchanged`` / ``completed`` straight from
        the port. ``unchanged`` performs no write, which is what keeps a
        scheduled re-sync quiet.
    """
    from skos.gtd_ingest import GtdCapture, upsert

    out: list[tuple[str, str, str]] = []
    with _gtd_store_at(home):
        for row in rows:
            item_id, action = upsert(
                GtdCapture(
                    text=gtd_text(row),
                    source=GTD_SOURCE,
                    source_ref=row.card_id,
                    context="@chef",
                    status="waiting",
                    meta={
                        "coord_card": row.card_id,
                        "coord_referent": row.needs,
                        "coord_system": row.system,
                        "coord_blocked_since": row.since,
                        "coord_unblocks": list(row.unblocks),
                    },
                )
            )
            out.append((row.card_id, item_id, action))
    return out
