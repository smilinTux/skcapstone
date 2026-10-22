"""Replay one guarded native link and its existing review-gate projection."""

from __future__ import annotations

import re
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from .card import CardEvent, CardEventLog
from .coord_completion import owned_revision_guard
from .review_verdict import _card_events
from .seraph_review_cardstore import LiveCardStoreGateway, _latest_outcome, card_revision
from .seraph_review_contracts import _digest


def record_guarded_link(
    home: Path,
    card_id: str,
    agent: str,
    key: str,
    value: str,
    *,
    expected_source_revision: str,
    expected_claim_revision: str,
    transition_id: str,
) -> dict:
    """Append once for an exact owned card; recover an interrupted projection.

    The native transition is durable first. The existing completion validator
    reads the sanctioned overlay, so mirror that same event identity exactly
    once, using CardEventLog rather than writing either store directly.
    A replay cannot adopt a later claim, contract, link set or outcome.
    """
    from .blocked_verdict import is_outcome_key

    guard = owned_revision_guard(
        home, agent, card_id, expected_source_revision, expected_claim_revision
    )
    if not isinstance(transition_id, str) or not re.fullmatch(r"[0-9a-f]{64}", transition_id):
        raise ValueError("guarded link transition invalid")
    request = dict(
        writer=agent,
        link_key=key,
        link_value=value,
        expected_source_revision=expected_source_revision,
        expected_claim_revision=expected_claim_revision,
    )
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
            raise ValueError("guarded link claim changed")
        matches = [
            row for row in store._read_events(card_id) if row.get("transition_id") == transition_id
        ]
        if matches:
            if len(matches) != 1:
                raise ValueError("guarded link ambiguous transition")
            event = matches[0]
            if (
                event.get("action") != "link"
                or any(event.get(name) != expected for name, expected in request.items())
                or card_revision(card) != event.get("guarded_after_card_revision")
            ):
                raise ValueError("guarded link replay changed")
        else:
            guard()
            after = card.model_copy(deep=True)
            after.links[key] = value
            event = store.append_event(
                card_id,
                "link",
                agent,
                link_key=key,
                link_value=value,
                expected_source_revision=expected_source_revision,
                expected_claim_revision=expected_claim_revision,
                guarded_after_card_revision=card_revision(after),
                guarded_prior_outcome_sha256=_digest(_latest_outcome(store, card_id)),
                transition_id=transition_id,
            )
        outcome = _latest_outcome(store, card_id)
        if (is_outcome_key(key) and outcome.get("event_id") != event["event_id"]) or (
            not is_outcome_key(key)
            and _digest(outcome) != event.get("guarded_prior_outcome_sha256")
        ):
            raise ValueError("guarded link outcome superseded")
        projected = CardEvent(
            card_id=card_id,
            **{
                name: event[name]
                for name in ("event_id", "writer", "ts", "seq", "action", "link_key", "link_value")
            },
        )
        projections = [
            row for row in _card_events(card_id, home) if row.get("event_id") == event["event_id"]
        ]
        if projections:
            if len(projections) != 1 or CardEvent.model_validate(projections[0]) != projected:
                raise ValueError("guarded link projection conflict")
        else:
            CardEventLog(home).append(projected)
        if _digest(_latest_outcome(CardStore(home), card_id)) != _digest(outcome):
            raise ValueError("guarded link outcome changed during projection")
        if card_revision(CardStore(home).fold(card_id)) != event["guarded_after_card_revision"]:
            raise ValueError("guarded link source changed during projection")
        return {
            "event_id": event["event_id"],
            "card_id": card_id,
            "source_revision": LiveCardStoreGateway(home).read_card(card_id).revision,
        }
