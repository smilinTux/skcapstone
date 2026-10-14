"""Operator proof for retiring released, unusable review generations."""

import json
import re
from pathlib import Path

from ..seraph_review_cardstore import _latest_outcome
from . import production_builder, review_retire
from .source_custody import CustodyState, inspect_custody


def prove(home, store, card, claim, owner, request_raw, status_raw, expected):
    """Validate exact operator preimages under the review mutation lock."""
    request, status = json.loads(request_raw), json.loads(status_raw)
    row = store.fold(card)
    source_id = request.get("source", {}).get("card")
    source = store.fold(source_id) if isinstance(source_id, str) else None
    exit_path = Path(home) / "evidence/production-review-exits" / f"{card}-{claim}.json"
    exit_raw = review_retire._raw(exit_path)
    terminal = json.loads(exit_raw)
    if row is None or source is None:
        raise ValueError("retirement pair missing")
    actual = {
        "card_sha256": production_builder.digest(row.model_dump(mode="json")),
        "source_sha256": production_builder.digest(source.model_dump(mode="json")),
        "request_sha256": review_retire.source_bundle._sha(request_raw),
        "status_sha256": review_retire.source_bundle._sha(status_raw),
        "exit_sha256": review_retire.source_bundle._sha(exit_raw),
    }
    if expected != actual:
        raise ValueError("operator retirement preimages changed")
    if (
        row.owner is not None
        or row.archived
        or row.status.value not in {"backlog", "ready", "review"}
        or {"hold", "do-not-claim", "human-gate"}.intersection(row.labels)
        or "review" not in row.labels
        or row.meta.get("link_source_card") != source_id
        or row.meta.get("link_head_revision") != request.get("source", {}).get("head")
        or request.get("card_id") != card
        or request.get("reviewer") != owner
        or status.get("card_id") != card
        or status.get("owner") != owner
        or status.get("claim_revision") != claim
        or status.get("state")
        not in {"running", "awaiting-review-acceptance", "review-fail", "review-blocked"}
        or inspect_custody(home, card, owner, claim, _card=row).state
        != CustodyState.RELEASED_CLEAN
    ):
        raise ValueError("released review custody changed")
    directory = Path(home) / "evidence/production-acceptance" / f"{card}-{claim}"
    # Imported Git objects do not seal an acceptance transaction. Keep them;
    # every context, journal, disposition or unknown root artifact still fences
    # retirement. The worker release and terminal proof remain mandatory.
    if directory.exists() and any(p.name != "imported-review" for p in directory.iterdir()):
        raise ValueError("review acceptance custody exists")
    execution = status.get("execution") or {}
    launches = [
        e
        for e in store._read_events(card)
        if e.get("action") == "review_assignment_launch"
        and e.get("claim_revision") == claim
        and e.get("launched") is True
    ]
    if (
        len(launches) != 1
        or launches[0].get("writer") != owner
        or launches[0].get("execution") != execution
        or execution.get("request_id") != request.get("request_id")
        or execution.get("request_sha256") != production_builder.digest(request)
        or terminal.get("schema") != "skfleet.production-review-exit/v1"
        or type(terminal.get("exit_code")) is not int
        or terminal.get("card") != card
        or terminal.get("owner") != owner
        or terminal.get("claim_revision") != claim
        or terminal.get("execution") != execution
        or any(terminal.get(k) != execution.get(k) for k in ("host", "unit", "invocation"))
        or terminal.get("source_head") != request.get("source", {}).get("head")
    ):
        raise ValueError("exact review terminal binding required")
    outcome = _latest_outcome(store, card)
    source_outcome = _latest_outcome(store, source_id)
    offered = request["source"]
    changed = (
        source_outcome.get("action") == "verdict"
        and any(
            source_outcome.get(k) != offered.get(v)
            for k, v in (
                ("writer", "owner"),
                ("candidate_commit", "head"),
                ("candidate_tree", "tree"),
                ("candidate_sha256", "evidence_sha256"),
            )
        )
    ) or (
        source_outcome.get("link_key") == "verdict_superseded"
        and str(source_outcome.get("link_value", "")).startswith("SUPERSEDED prior_event=")
    )
    if outcome and not changed:
        raise ValueError("current review outcome must remain available for acceptance")
    terminal_state = review_retire.unit_terminal(
        execution["unit"], execution["invocation"], host=execution["host"]
    )
    if (
        production_builder.digest(store.fold(card).model_dump(mode="json"))
        != actual["card_sha256"]
        or production_builder.digest(store.fold(source_id).model_dump(mode="json"))
        != actual["source_sha256"]
    ):
        raise ValueError("retirement pair changed during terminal check")
    return {
        "schema": "skfleet.operator-review-generation-retirement/v1",
        "expected": expected,
        "review": {"card": card, "owner": owner, "claim": claim},
        "reason": "source-generation-changed" if changed else "released-without-native-outcome",
        "terminal": terminal,
        "terminal_state": terminal_state,
        "source_outcome": source_outcome,
        "review_outcome": outcome,
    }


def retire(home, card, claim, owner, expected, *, actor, reason, apply=False):
    """Run the existing supersession writer, or finish its exact lost reply."""
    import socket

    from skcoord.card_store import CardStore, card_mutation_lock

    from .paths import FleetPaths

    home = Path(home)
    policy = production_builder.policy()
    if not policy or policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production authority required")
    if (
        not review_retire.CARD.fullmatch(card)
        or not re.fullmatch(r"[0-9a-f]{32}", claim)
        or not review_retire.dispatch.valid_name(actor)
        or not review_retire.dispatch.valid_name(owner)
        or not 1 <= len(reason.strip()) <= 1024
    ):
        raise ValueError("exact operator identity and reason required")
    paths = FleetPaths(home / "fleet")
    store = CardStore(home)
    events = store._read_events(card)
    previous = [
        e
        for e in events
        if e.get("action") == "remote_review_supersede"
        and e.get("released_owner") == owner
        and e.get("released_claim") == claim
    ]
    if previous:
        if len(previous) != 1:
            raise ValueError("retirement replay history changed")
        event = previous[0]
        request_id = event["request_id"]
        receipt = review_retire._supersession_receipt(home, card, request_id)
        proof = json.loads(review_retire._raw(receipt.parent / "context.json"))
        request = json.loads(review_retire._raw(receipt.parent / "request.json"))
        request_path = review_retire.dispatch.request_path(paths, request["node"], card)
        status_path = review_retire.dispatch.status_path(paths, request["node"], card)
        with (
            review_retire.dispatch._request_exclusion(paths.root / "dispatch/.production-offer"),
            review_retire.dispatch._request_exclusion(request_path),
            card_mutation_lock(home, card),
        ):
            if (
                event.get("actor") != actor
                or event.get("reason") != reason
                or proof.get("expected") != expected
                or request_id
                not in review_retire.retired_offers(home, card, store._read_events(card))
            ):
                raise ValueError("retirement replay proof changed")
            for path, key in ((request_path, "request_sha256"), (status_path, "status_sha256")):
                if (
                    path.exists()
                    and review_retire.source_bundle._sha(review_retire._raw(path)) != expected[key]
                ):
                    raise ValueError("retired pointer replaced by a new generation")
            if apply:
                review_retire._remove_pointer(status_path)
                review_retire._remove_pointer(request_path)
        return "already-retired"
    return review_retire.supersede_generation(
        paths,
        home,
        card,
        claim,
        owner,
        actor=actor,
        reason=reason,
        raw_context=b"",
        operator_expected=expected,
        check_only=not apply,
    )
