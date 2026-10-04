"""Retire one failed remote review offer with no completed review custody."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..seat_runtime import governed_review_assignment_ready, review_state_revision
from . import builder_dispatch as dispatch
from . import production_builder, source_bundle
from .production_review_custody import unit_terminal

SCHEMA = "skfleet.remote-review-retirement/v1"
SHA = re.compile(r"[0-9a-f]{64}")
CARD = re.compile(r"[0-9a-f]{8}")


def _raw(path: Path) -> bytes:
    return source_bundle._read(path, source_bundle.MAX_EVIDENCE)


def _receipt_path(home: Path, card: str, request_id: str) -> Path:
    if not CARD.fullmatch(card) or not SHA.fullmatch(request_id):
        raise ValueError("invalid review retirement identity")
    return home / "evidence/work" / card / "retired-reviews" / request_id / "retirement.json"


def retired_offers(home: Path, card: str, events: list[dict]) -> set[str]:
    """Accept a historical offer only with its exact native retirement and archive."""
    retired = set()
    for event in events:
        if event.get("action") != "remote_review_retire" or event.get("schema") != SCHEMA:
            continue
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not SHA.fullmatch(request_id):
            continue
        receipt = _receipt_path(home, card, request_id)
        try:
            raw_receipt = _raw(receipt)
            value = json.loads(raw_receipt)
            archived = _raw(receipt.parent / "request.json")
            archived_request = json.loads(archived)
            archived_status = _raw(receipt.parent / "status.json")
            archived_exit = _raw(receipt.parent / "worker-exit.json")
        except (OSError, ValueError):
            continue
        offers = [
            row
            for row in events
            if row.get("action") == "remote_review_offer"
            and row.get("request_id") == request_id
            and row.get("request_sha256") == event.get("offer_request_sha256")
        ]
        if (
            len(offers) == 1
            and event.get("writer") == event.get("actor")
            and dispatch.valid_name(event.get("actor", ""))
            and value.get("schema") == SCHEMA
            and value.get("card_id") == card
            and value.get("request_id") == request_id
            and value.get("request_sha256") == event.get("request_sha256")
            and source_bundle._sha(archived) == event.get("request_sha256")
            and production_builder.digest(archived_request) == event.get("offer_request_sha256")
            and value.get("status_sha256") == event.get("status_sha256")
            and source_bundle._sha(archived_status) == event.get("status_sha256")
            and source_bundle._sha(archived_exit) == value.get("exit_sha256")
            and source_bundle._sha(raw_receipt) == event.get("receipt_sha256")
        ):
            retired.add(request_id)
    return retired


def _remove_pointer(path: Path) -> None:
    if path.exists():
        path.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def retire(
    paths,
    home: Path,
    node: str,
    card: str,
    *,
    request_sha256: str,
    status_sha256: str,
    card_sha256: str,
    actor: str,
    reason: str,
    apply: bool = False,
) -> dict:
    """Archive an exact failed generation, then clear only its stale pointers."""
    home = Path(home)
    policy = production_builder.policy()
    if not policy or policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production authority required")
    if (
        not dispatch.valid_name(node)
        or not CARD.fullmatch(card)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", actor)
        or not 1 <= len(reason.strip()) <= 1024
        or any(not SHA.fullmatch(v) for v in (request_sha256, status_sha256, card_sha256))
    ):
        raise ValueError("exact retirement identity and attribution required")
    request_path = dispatch.request_path(paths, node, card)
    status_path = dispatch.status_path(paths, node, card)
    with (
        dispatch._request_exclusion(paths.root / "dispatch/.production-offer"),
        dispatch._request_exclusion(request_path),
        card_mutation_lock(home, card),
    ):
        store = CardStore(home)
        current = store.fold(card)
        if (
            not governed_review_assignment_ready(current)
            or review_state_revision(current) != card_sha256
            or current.archived
            or current.meta.get("claim_conflicts")
            or {"hold", "human-gate"}.intersection(current.labels)
        ):
            raise ValueError("unclaimed review card changed")
        events = store._read_events(card)
        previous = [
            e
            for e in events
            if e.get("action") == "remote_review_retire"
            and e.get("request_sha256") == request_sha256
            and e.get("status_sha256") == status_sha256
            and e.get("card_sha256") == card_sha256
        ]
        if previous:
            if len(previous) != 1 or previous[0].get("request_id") not in retired_offers(
                home, card, events
            ):
                raise ValueError("previous retirement proof differs")
            for path, expected in ((request_path, request_sha256), (status_path, status_sha256)):
                if path.exists() and source_bundle._sha(_raw(path)) != expected:
                    raise ValueError("retired pointer changed")
            if apply:
                _remove_pointer(status_path)
                _remove_pointer(request_path)
            return {
                "state": "already-retired",
                "receipt": str(_receipt_path(home, card, previous[0]["request_id"])),
            }
        raw_request = _raw(request_path)
        raw_status = _raw(status_path)
        if (
            source_bundle._sha(raw_request) != request_sha256
            or source_bundle._sha(raw_status) != status_sha256
        ):
            raise ValueError("request or status hash changed")
        request, status = json.loads(raw_request), json.loads(raw_status)
        execution = status.get("execution") or {}
        host = production_builder.node_binding(paths, node, policy)["host"]
        if (
            request.get("schema") != "skfleet.builder-dispatch/v2"
            or request.get("work_kind") != "review"
            or status.get("schema") != "skfleet.builder-dispatch-status/v1"
            or status.get("work_kind") != "review"
            or any(request.get(key) != value for key, value in (("card_id", card), ("node", node)))
            or any(
                status.get(key) != request.get(key) for key in ("card_id", "node", "request_id")
            )
            or status.get("state") != "running"
            or status.get("review_packet") is not None
            or status.get("terminal") is not None
            or execution.get("host") != host
            or execution.get("request_id") != request["request_id"]
            or execution.get("request_sha256") != production_builder.digest(request)
            or not re.fullmatch(r"[0-9a-f]{32}", str(status.get("claim_revision", "")))
        ):
            raise ValueError("exact failed remote review generation required")
        request_id = request["request_id"]
        if not SHA.fullmatch(request_id) or request_id in retired_offers(home, card, events):
            raise ValueError("review generation already retired or malformed")
        offers = [
            e
            for e in events
            if e.get("action") == "remote_review_offer" and e.get("request_id") == request_id
        ]
        launches = [
            e
            for e in events
            if e.get("action") == "review_assignment_launch"
            and e.get("recommendation_id") == request_id
        ]
        releases = [
            e
            for e in events
            if e.get("action") == "release_claim"
            and e.get("released_owner") == status.get("owner")
            and e.get("expected_claim_revision") == status["claim_revision"]
        ]
        if (
            len(offers) != 1
            or offers[0].get("request_sha256") != production_builder.digest(request)
            or len(launches) != 1
            or launches[0].get("claim_revision") != status["claim_revision"]
            or launches[0].get("execution") != execution
            or launches[0].get("writer") != status["owner"]
            or launches[0].get("launched") is not True
            or not releases
            or status.get("owner") != request.get("reviewer")
        ):
            raise ValueError("native offer, launch or claim release proof missing")
        exit_paths = sorted((home / "evidence/worker-exits").glob(card + "-*.json"))
        exits = []
        for path in exit_paths:
            try:
                raw_exit = _raw(path)
                value = json.loads(raw_exit)
            except (OSError, ValueError):
                continue
            if (
                value.get("card_id") == card
                and value.get("owner") == status["owner"]
                and value.get("claim_revision") == status["claim_revision"]
                and value.get("host") == host
                and type(value.get("child_exit_code")) is int
                and value["child_exit_code"] != 0
            ):
                exits.append((raw_exit, path))
        if len(exits) != 1:
            raise ValueError("one exact failed worker exit required")
        unit = unit_terminal(execution["unit"], execution["invocation"], host=host)
        if (
            review_state_revision(store.fold(card)) != card_sha256
            or _raw(request_path) != raw_request
            or _raw(status_path) != raw_status
        ):
            raise ValueError("review generation changed during terminal check")
        binding = dict(
            schema=SCHEMA,
            card_id=card,
            node=node,
            request_id=request_id,
            request_sha256=request_sha256,
            status_sha256=status_sha256,
            offer_request_sha256=production_builder.digest(request),
            card_sha256=card_sha256,
            exit_sha256=source_bundle._sha(exits[0][0]),
            unit=execution["unit"],
            invocation=execution["invocation"],
            unit_state=unit,
            actor=actor,
            reason=reason.strip(),
        )
        if not apply:
            return {"state": "qualified-check-only", "binding": binding}
        receipt = _receipt_path(home, card, request_id)
        source_bundle._once(receipt.parent / "request.json", raw_request)
        source_bundle._once(receipt.parent / "status.json", raw_status)
        source_bundle._once(receipt.parent / "worker-exit.json", exits[0][0])
        source_bundle._once(receipt, json.dumps(binding, sort_keys=True).encode())
        store.append_event(
            card,
            "remote_review_retire",
            actor,
            schema=SCHEMA,
            request_id=request_id,
            request_sha256=request_sha256,
            offer_request_sha256=production_builder.digest(request),
            status_sha256=status_sha256,
            card_sha256=card_sha256,
            actor=actor,
            reason=reason.strip(),
            receipt_sha256=source_bundle._sha(_raw(receipt)),
        )
        # Remove the node status first: a crash leaves the request holding the card.
        _remove_pointer(status_path)
        _remove_pointer(request_path)
        return {"state": "retired", "receipt": str(receipt)}
