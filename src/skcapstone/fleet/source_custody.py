"""One locked custody snapshot for held and cleanly released source claims."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from skcoord.card_store import CardStore, card_mutation_lock

from ..seraph_review_cardstore import LiveCardStoreGateway, _binding, _latest_outcome


class CustodyState(str, Enum):
    """Disposition of one exact producer claim."""

    HELD = "HELD"
    RELEASED_CLEAN = "RELEASED_CLEAN"
    CHANGED = "CHANGED"


class EventSnapshot(CardStore):
    """Read each source ledger once; delegate unrelated cards to the native store."""

    def __init__(self, home: Path, card_id: str):
        super().__init__(home)
        self.card_id = card_id
        self.native = super()._read_events(card_id)
        self.legacy = super()._legacy_events(card_id)

    def _read_events(self, card_id):
        return list(self.native) if card_id == self.card_id else super()._read_events(card_id)

    def _legacy_events(self, card_id):
        return list(self.legacy) if card_id == self.card_id else super()._legacy_events(card_id)


class BeforeRelease(CardStore):
    """Read-only view excluding exactly the proven release, never later changes."""

    def __init__(self, store: CardStore, release: dict, *, at_release: bool = False):
        super().__init__(store.home)
        self.store = store
        self.release = release
        self.at_release = at_release

    def _read_events(self, card_id):
        return [e for e in self.store._read_events(card_id) if self._retained(e, card_id)]

    def _legacy_events(self, card_id):
        return [e for e in self.store._legacy_events(card_id) if self._retained(e, card_id)]

    def _retained(self, event, card_id):
        """Optionally capture the binding at release for a new review opener."""
        return event != self.release and (
            not self.at_release
            or card_id != self.store.card_id
            or _position(event) < _position(self.release)
        )


@dataclass(frozen=True)
class CustodyResult:
    """Custody decision, diagnostic and exact immutable-read evidence."""

    state: CustodyState
    reason: str
    store: EventSnapshot
    card: Any
    release: dict | None = None

    def before_release(self) -> BeforeRelease:
        """Return a counterfactual only for a proven clean release."""
        if self.state != CustodyState.RELEASED_CLEAN or self.release is None:
            raise ValueError("source has no clean release")
        view = BeforeRelease(self.store, self.release)
        row = view.fold(self.card.id)
        if (
            row.owner != self.release["released_owner"]
            or row.meta.get("_claim_revision") != self.release["expected_claim_revision"]
        ):
            raise ValueError("release did not relinquish the captured claim")
        return view


def _position(event: dict) -> tuple:
    """Match native custody ordering."""
    return event.get("ts", ""), event.get("writer", ""), event.get("seq", 0)


def _candidate_binding_view(store, row, source, events):
    """Exclude only proven pre-claim content links superseded by the typed proposal.

    Repository and metadata remain strict. A later link, an unrecorded value or
    an ambiguous producer claim cannot be explained away as historical state.
    """
    core = row.model_copy(deep=True)
    claims = [
        event
        for event in store._read_events(row.id)
        if event.get("action") == "claim"
        and (event.get("claim_revision") or event.get("event_id")) == source["claim"]
        and event.get("owner") == source["owner"]
    ]
    if len(claims) != 1:
        return core
    keys = {
        **dict.fromkeys(
            (
                "link_head_revision",
                "head",
                "head_commit",
                "commit",
                "commit_sha",
                "candidate_commit",
            ),
            source["head"],
        ),
        **dict.fromkeys(("tree", "candidate_tree"), source["tree"]),
    }
    for key, expected in keys.items():
        value = core.links.get(key)
        if value is None or value == expected:
            continue
        updates = [
            event
            for event in events
            if event.get("action") == "link" and (event.get("link_key") or event.get("key")) == key
        ]
        latest = max(updates, key=_position, default={})
        if (
            latest.get("ts")
            and claims[0].get("ts")
            and _position(latest) < _position(claims[0])
            and latest.get("link_value", latest.get("value")) == value
        ):
            del core.links[key]
    return core


def candidate_matches(store: CardStore, row: Any, source: dict) -> bool:
    """Verify the retained typed proposal and current outcome/content bindings."""
    events = store._read_events(row.id) + store._legacy_events(row.id)
    outcome = max((e for e in events if e.get("action") == "verdict"), key=_position, default={})
    current = _latest_outcome(store, row.id)
    if (
        (current.get("verdict") or current.get("link_value")) not in {"PASS_FOR_REVIEW", "PASS"}
        or outcome.get("verdict") != "PASS_FOR_REVIEW"
        or outcome.get("writer") != source["owner"]
        or outcome.get("expected_claim_revision") not in (None, source["claim"])
        or any(
            outcome.get(field) != source[key]
            for field, key in (
                ("candidate_commit", "head"),
                ("candidate_tree", "tree"),
                ("candidate_ref", "ref"),
                ("candidate_sha256", "evidence_sha256"),
            )
        )
    ):
        return False
    try:
        row = _candidate_binding_view(store, row, source, events)
        return (
            _binding(row, "repository") == source["repository"]
            and _binding(
                row,
                "link_head_revision",
                "head",
                "head_commit",
                "commit",
                "commit_sha",
                "candidate_commit",
            )
            in (None, source["head"])
            and _binding(row, "tree", "candidate_tree") in (None, source["tree"])
        )
    except ValueError:
        return False


def inspect_custody(
    home: Path,
    card_id: str,
    owner: str,
    claim: str,
    *,
    held_statuses: frozenset[str] = frozenset({"ready", "doing", "review"}),
    source: dict | None = None,
    revision: str | None = None,
    _card: Any = None,
) -> CustodyResult:
    """Classify a locked source; writers must retain their enclosing card lock.

    Admission before publication omits source content. Acceptance supplies its
    sealed proposal. Each caller retains its existing held-status policy.
    _card reuses a caller's authoritative fold under the enclosing card lock.
    """
    with card_mutation_lock(home, card_id):
        store = EventSnapshot(home, card_id)
        row = _card if _card is not None else store.fold(card_id)

        def result(state, reason="", release=None):
            return CustodyResult(state, reason, store, row, release)

        if row is None or row.archived or row.meta.get("claim_conflicts"):
            return result(CustodyState.CHANGED, "source missing, archived or conflicted")
        if revision is not None and (
            LiveCardStoreGateway(home).read_card(card_id, _store=store).revision != revision
        ):
            return result(CustodyState.CHANGED, "source revision changed")
        if (
            row.owner == owner
            and row.meta.get("_claim_revision") == claim
            and row.status.value in held_statuses
        ):
            return result(CustodyState.HELD)
        if row.owner is not None or row.status.value not in {"backlog", "ready", "review"}:
            return result(CustodyState.CHANGED, "source claim changed")
        events = store.native + store.legacy
        custody = [
            e
            for e in events
            if e.get("action")
            in {"claim", "release_claim", "assign", "unassign", "complete", "void"}
        ]
        last = sorted(custody, key=_position)[-1] if custody else {}
        if (
            last.get("action") != "release_claim"
            or last.get("released_owner") != owner
            or last.get("expected_claim_revision") != claim
        ):
            return result(CustodyState.CHANGED, "latest custody is not this claim's release")
        if source is not None and not candidate_matches(store, row, source):
            return result(CustodyState.CHANGED, "source candidate or outcome changed")
        return result(CustodyState.RELEASED_CLEAN, release=last)


def sealed_source_outcome(store, card_id, owner, claim, head, tree):
    """Find the exact producer PASS bound by the immutable review source bundle."""
    from .production_review_evidence import ReviewEvidenceError

    rows = store._read_events(card_id) + store._legacy_events(card_id)
    claims = [
        event
        for event in rows
        if event.get("action") == "claim"
        and (event.get("claim_revision") or event.get("event_id")) == claim
        and event.get("owner") == owner
    ]
    outcome = _latest_outcome(store, card_id)
    if (
        len(claims) != 1
        or outcome.get("action") != "verdict"
        or outcome.get("verdict") != "PASS_FOR_REVIEW"
        or outcome.get("writer") != owner
        or outcome.get("expected_claim_revision") not in (None, claim)
        or outcome.get("candidate_commit") != head
        or outcome.get("candidate_tree") != tree
        or outcome.get("ts", "") < claims[0].get("ts", "")
    ):
        raise ReviewEvidenceError("sealed producer outcome is unavailable")
    return outcome
