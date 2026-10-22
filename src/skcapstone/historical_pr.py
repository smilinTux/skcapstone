"""Archive only an exact historical PR/repository mismatch, preserving outcomes."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from skcoord.card_store import CardStore, card_mutation_lock

from .card import CardEvent, CardEventLog
from .review_verdict import _card_events
from .seraph_review_cardstore import _binding, _candidate, _latest_outcome
from .seraph_review_contracts import ReviewPublicationError, _digest

LINEAGE_KEY = "historical_pr_association"


def raw_card_revision(card: object) -> str:
    """Hash every folded fact except the event-derived update timestamp."""
    payload = card.model_dump(mode="json")
    payload.pop("updated_at", None)
    return _digest(payload)


def history_revision(
    store: CardStore, card_id: str, excluded: set[str] | frozenset[str] = frozenset()
) -> str:
    """Pin all native and legacy history, excluding this operation on replay."""
    # The fold's normalized overlay rows discard event IDs. Pin the original
    # supported overlay projection instead so replay excludes only our IDs.
    rows = (
        store._read_events(card_id)
        + list(_card_events(card_id, store.home))
        + [row for row in store._legacy_events(card_id) if row.get("origin") != "legacy-overlay"]
    )
    return _digest(sorted(_digest(row) for row in rows if row.get("event_id") not in excluded))


def _project(home: Path, card_id: str, event: dict) -> None:
    """Mirror the durable native event once through the supported event log."""
    projected = CardEvent(
        card_id=card_id,
        **{
            name: event[name]
            for name in ("event_id", "writer", "ts", "seq", "action", "link_key", "link_value")
        },
    )
    matches = [
        row for row in _card_events(card_id, home) if row.get("event_id") == event["event_id"]
    ]
    if matches:
        if len(matches) != 1 or CardEvent.model_validate(matches[0]) != projected:
            raise ValueError("historical PR projection conflict")
    else:
        CardEventLog(home).append(projected)


def archive_historical_pr(
    home: Path,
    card_id: str,
    agent: str,
    *,
    expected_card_revision: str,
    expected_history_revision: str,
    expected_claim_revision: str,
    expected_pr: str,
    expected_repository: str,
    transition_id: str,
) -> dict:
    """Retain lineage then clear only the active PR; recover either lost reply.

    This deliberately does not weaken the normal source snapshot guard. Only
    the specific PR/repository mismatch is admissible, and both its raw fold
    and complete history must match caller-supplied preimages under the lock.
    No metadata PR, conflicting head, outcome, or claim may be repaired here.
    """
    for value, length in (
        (expected_card_revision, 64),
        (expected_history_revision, 64),
        (expected_claim_revision, 32),
        (transition_id, 64),
    ):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{%d}" % length, value):
            raise ValueError("historical PR guard invalid")
    repository_url = urlsplit(expected_repository)
    if (
        repository_url.scheme != "https"
        or not repository_url.hostname
        or repository_url.username
        or repository_url.password
        or repository_url.query
        or repository_url.fragment
        or not repository_url.path.strip("/")
    ):
        raise ValueError("historical PR repository must be credential-free HTTPS")
    request = dict(
        card_id=card_id,
        owner=agent,
        claim_revision=expected_claim_revision,
        before_card_revision=expected_card_revision,
        before_history_revision=expected_history_revision,
        pr=expected_pr,
        repository=expected_repository,
        transition_id=transition_id,
    )
    lineage = json.dumps(request, sort_keys=True, separators=(",", ":"))
    steps = [(LINEAGE_KEY, lineage), ("pr", "")]
    tokens = [_digest({"historical_pr": transition_id, "step": i}) for i in range(2)]
    with card_mutation_lock(home, card_id):
        store = CardStore(home)
        card = store.fold(card_id)
        if (
            card is None
            or card.owner != agent
            or card.archived
            or card.meta.get("_claim_revision") != expected_claim_revision
            or card.meta.get("claim_conflicts")
            or card.status.value not in {"ready", "doing", "review"}
        ):
            raise ValueError("historical PR claim changed")
        events = []
        for token, (key, value) in zip(tokens, steps):
            matches = [
                row for row in store._read_events(card_id) if row.get("transition_id") == token
            ]
            if len(matches) > 1 or (
                matches
                and any(
                    matches[0].get(name) != wanted
                    for name, wanted in dict(
                        action="link",
                        writer=agent,
                        link_key=key,
                        link_value=value,
                        historical_pr_request=request,
                    ).items()
                )
            ):
                raise ValueError("historical PR replay changed")
            events.append(matches[0] if matches else None)
        if events[1] and not events[0]:
            raise ValueError("historical PR lineage missing")
        prior = card.model_copy(deep=True)
        for event, (key, value) in zip(events, steps):
            if event:
                if prior.links.get(key) != value:
                    raise ValueError("historical PR lineage changed")
                if key == LINEAGE_KEY:
                    del prior.links[key]
                else:
                    prior.links[key] = expected_pr
        own_ids = {event["event_id"] for event in events if event}
        if (
            raw_card_revision(prior) != expected_card_revision
            or history_revision(store, card_id, own_ids) != expected_history_revision
        ):
            raise ValueError("historical PR preimage changed")
        if (
            LINEAGE_KEY in prior.links
            or LINEAGE_KEY in prior.meta
            or "pr" in prior.meta
            or prior.links.get("pr") != expected_pr
            or _binding(prior, "repository") != expected_repository
        ):
            raise ValueError("historical PR binding changed")
        try:
            _candidate(prior)
        except ReviewPublicationError as exc:
            if str(exc) != "card_pr_repository_mismatch":
                raise
        else:
            raise ValueError("historical PR is not repository-mismatched")
        after = prior.model_copy(deep=True)
        after.links.update(dict(steps))
        _candidate(after)  # Reject another binding error before the first write.
        outcome = _digest(_latest_outcome(store, card_id))
        for index, (key, value) in enumerate(steps):
            if events[index] is None:
                events[index] = store.append_event(
                    card_id,
                    "link",
                    agent,
                    link_key=key,
                    link_value=value,
                    transition_id=tokens[index],
                    historical_pr_request=request,
                )
            _project(home, card_id, events[index])
        refreshed = CardStore(home)
        own_ids = {event["event_id"] for event in events}
        if (
            raw_card_revision(refreshed.fold(card_id)) != raw_card_revision(after)
            or history_revision(refreshed, card_id, own_ids) != expected_history_revision
            or _digest(_latest_outcome(refreshed, card_id)) != outcome
        ):
            raise ValueError("historical PR state changed during projection")
        return dict(
            card_id=card_id,
            card_revision=raw_card_revision(after),
            history_revision=history_revision(refreshed, card_id),
            event_ids=[event["event_id"] for event in events],
            lineage_key=LINEAGE_KEY,
            prior_outcome_sha256=outcome,
        )
