"""Role-bounded lifecycle fan-out through the Niobe dispatcher."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from .card_store import Card, CardStore
from .niobe_activation import LIVE_UNIT, parse_activation
from .skrsi_estate_adapters import DEFAULT_CHILD_MODEL, SEAT_CHILD_ROUTES


class FanoutBoundaryError(ValueError):
    """A lifecycle request or receipt failed its authority fence."""


_HEAD = re.compile(r"[0-9a-f]{40}")
_REQUESTERS = frozenset({"link", "mero"})
_TRANSITIONS = frozenset(
    {
        "reserved",
        "materialized",
        "claimed",
        "launched",
        "launch_failed",
        "occupied",
        "stopped",
        "released",
        "reassigned",
        "retired",
    }
)


@dataclass(frozen=True)
class NiobeRuntimeIdentity:
    """Identity derived from the active decision and the running systemd unit."""

    decision_id: str
    card_revision: str
    host: str
    unit: str


@dataclass(frozen=True)
class RequestingSeatIdentity:
    """Link or Mero identity derived from the active systemd seat runtime."""

    seat: str
    host: str
    control_revision: str
    unit: str


def resolve_requesting_seat_identity(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
    cgroup_text: str | None = None,
) -> RequestingSeatIdentity:
    """Resolve the requesting seat from control-plane and process truth."""

    values = os.environ if environ is None else environ
    seat = values.get("SKAGENT", "").strip().lower()
    if seat not in _REQUESTERS or values.get("SKCAPSTONE_AGENT", "").strip().lower() != seat:
        raise FanoutBoundaryError("requesting runtime is not Link or Mero")
    invocation = values.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", invocation):
        raise FanoutBoundaryError("requesting runtime has no verified systemd invocation")
    service = f"skfleet-{seat}.service"
    process_cgroup = (
        Path("/proc/self/cgroup").read_text(encoding="utf-8")
        if cgroup_text is None
        else cgroup_text
    )
    if service not in process_cgroup:
        raise FanoutBoundaryError("requesting runtime is outside its authorized service")
    from .seat_cycle_entrypoint import load_control_plane

    control = load_control_plane(home / "coordination" / "seat-control-plane.json")
    host = (hostname or socket.gethostname()).strip().lower()
    if host != str(control["active_host"]) or host not in control["seats"][seat]:
        raise FanoutBoundaryError("requesting runtime host is not authorized")
    return RequestingSeatIdentity(seat, host, str(control["revision"]), service)


def resolve_niobe_runtime_identity(
    home: Path,
    *,
    environ: Mapping[str, str] | None = None,
    hostname: str | None = None,
    cgroup_text: str | None = None,
) -> NiobeRuntimeIdentity:
    """Resolve Niobe from activation and process truth, never a caller label."""

    values = os.environ if environ is None else environ
    expected = (home / "coordination" / "niobe-activation.json").resolve()
    supplied = Path(values.get("SKFLEET_NIOBE_ACTIVATION", "")).expanduser()
    if not supplied or supplied.resolve() != expected or not expected.is_file():
        raise FanoutBoundaryError("Niobe runtime activation is missing")
    activation = parse_activation(json.loads(expected.read_text(encoding="utf-8")))
    actual_host = (hostname or socket.gethostname()).strip().lower()
    if actual_host != activation.host:
        raise FanoutBoundaryError("Niobe runtime host is not authorized")
    core = home / "cards" / activation.card_id / "core.json"
    if (
        not core.is_file()
        or hashlib.sha256(core.read_bytes()).hexdigest() != activation.card_revision
    ):
        raise FanoutBoundaryError("Niobe runtime activation card is stale")
    invocation = values.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", invocation):
        raise FanoutBoundaryError("Niobe runtime has no verified systemd invocation")
    process_cgroup = (
        Path("/proc/self/cgroup").read_text(encoding="utf-8")
        if cgroup_text is None
        else cgroup_text
    )
    service = LIVE_UNIT.removesuffix(".timer") + ".service"
    if service not in process_cgroup:
        raise FanoutBoundaryError("Niobe runtime is outside its authorized service")
    return NiobeRuntimeIdentity(
        activation.decision_id,
        activation.card_revision,
        activation.host,
        LIVE_UNIT,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


@dataclass(frozen=True)
class LifecycleFanoutRequest:
    """One advisory Link or Mero request for an existing lifecycle card."""

    card_id: str
    source_head: str
    requester: str
    route: str
    model: str = DEFAULT_CHILD_MODEL
    request_id: str = ""

    def normalized(self) -> "LifecycleFanoutRequest":
        """Return the request with its deterministic identity filled in."""

        values = {
            "card_id": self.card_id.strip().lower(),
            "source_head": self.source_head.strip().lower(),
            "requester": self.requester.strip().lower(),
            "route": self.route.strip().lower(),
            "model": self.model.strip(),
        }
        return LifecycleFanoutRequest(**values, request_id=_digest(values))

    def validate(self, card: Card, *, require_unclaimed: bool = True) -> None:
        """Intersect the requester role with the card's explicit route scope."""

        request = self.normalized()
        if self != request:
            raise FanoutBoundaryError("fan-out request is not canonical")
        if request.requester not in _REQUESTERS:
            raise FanoutBoundaryError("only Link or Mero may request lifecycle fan-out")
        if not _HEAD.fullmatch(request.source_head):
            raise FanoutBoundaryError("source head must be an exact lowercase Git SHA")
        if request.route not in SEAT_CHILD_ROUTES[request.requester]:
            raise FanoutBoundaryError("requested route exceeds the requester role")
        if request.model != DEFAULT_CHILD_MODEL:
            raise FanoutBoundaryError("fan-out model must use the bounded default")
        labels = {str(label).strip().lower() for label in card.labels}
        if f"fanout-scope-{request.route}" not in labels:
            raise FanoutBoundaryError("requested route exceeds the card scope")
        if card.id != request.card_id:
            raise FanoutBoundaryError("request card identity changed")
        if require_unclaimed and card.owner is not None:
            raise FanoutBoundaryError("fan-out request requires an unclaimed card")

    def as_event(self) -> dict[str, object]:
        """Return the closed event payload."""

        return {"schema": "skfleet.niobe-fanout-request/v1", **asdict(self)}


def submit_fanout_request(home: Path, request: LifecycleFanoutRequest) -> LifecycleFanoutRequest:
    """Append one idempotent advisory request without claiming or launching."""

    request = request.normalized()
    identity = resolve_requesting_seat_identity(home)
    if request.requester != identity.seat:
        raise FanoutBoundaryError("fan-out requester does not match verified runtime identity")
    store = CardStore(home)
    card = store.fold(request.card_id)
    if card is None:
        raise FanoutBoundaryError("fan-out card is missing")
    request.validate(card)
    payload = request.as_event()
    payload.pop("card_id")
    payload.pop("requester")
    store.append_event(
        request.card_id,
        "niobe_fanout_request",
        request.requester,
        transition_id=request.request_id,
        requester_authority_revision=identity.control_revision,
        requester_host=identity.host,
        requester_unit=identity.unit,
        **payload,
    )
    return request


def _request_from_event(card_id: str, event: Mapping[str, object]) -> LifecycleFanoutRequest:
    return LifecycleFanoutRequest(
        card_id=card_id,
        source_head=str(event.get("source_head") or ""),
        requester=str(event.get("writer") or ""),
        route=str(event.get("route") or ""),
        model=str(event.get("model") or ""),
        request_id=str(event.get("request_id") or ""),
    )


def pending_fanout_request(home: Path, card_id: str) -> LifecycleFanoutRequest | None:
    """Atomically reserve the one request the verified Niobe runtime may execute."""

    store = CardStore(home)
    rows = store._read_events(card_id)
    request_rows = [row for row in rows if row.get("action") == "niobe_fanout_request"]
    if not request_rows:
        return None
    authority = resolve_niobe_runtime_identity(home)

    # The board lock is the existing global CardStore mutation boundary. Niobe
    # is activated on one host, so this serializes the cross-card scan and the
    # reservation append before any materialization or claim can begin.
    from skcoord.coordination import _board_mutation_lock

    with _board_mutation_lock(home):
        rows = store._read_events(card_id)
        request_rows = [row for row in rows if row.get("action") == "niobe_fanout_request"]
        if request_rows[-1].get("schema") != "skfleet.niobe-fanout-request/v1":
            raise FanoutBoundaryError("fan-out request schema is invalid")
        request = _request_from_event(card_id, request_rows[-1])
        card = store.fold(card_id)
        if card is None:
            raise FanoutBoundaryError("fan-out card is missing")
        request.validate(card, require_unclaimed=False)
        matching = {
            str(row.get("request_id") or "")
            for row in request_rows
            if str(row.get("source_head") or "") == request.source_head
        }
        if matching != {request.request_id}:
            raise FanoutBoundaryError("source head has ambiguous requests")

        matching_receipts: list[tuple[str, Mapping[str, object]]] = []
        for other_id in store.list_card_ids():
            for row in store._read_events(other_id):
                if (
                    row.get("action") == "niobe_fanout_receipt"
                    and row.get("source_head") == request.source_head
                ):
                    matching_receipts.append((other_id, row))
        if any(
            other_id != card_id or row.get("request_id") != request.request_id
            for other_id, row in matching_receipts
        ):
            return None
        prior = matching_receipts[-1][1] if matching_receipts else None
        if prior and prior.get("state") not in {"launch_failed", "released"}:
            return None
        if card.owner is not None:
            raise FanoutBoundaryError("fan-out request requires an unclaimed card")
        reservation_generation = _digest(
            {
                "request_id": request.request_id,
                "prior_event": prior.get("event_id") if prior else "initial",
                "authority_revision": authority.card_revision,
            }
        )
        append_fanout_receipt(
            home,
            request,
            state="reserved",
            process={
                "authority_decision": authority.decision_id,
                "authority_revision": authority.card_revision,
                "reservation_generation": reservation_generation,
            },
        )
        return request


def append_fanout_receipt(
    home: Path,
    request: LifecycleFanoutRequest,
    *,
    state: str,
    claim_owner: str = "",
    claim_revision: str = "",
    process: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Append one immutable Niobe transition receipt for an exact request."""

    normalized_state = state.strip().lower()
    if normalized_state not in _TRANSITIONS:
        raise FanoutBoundaryError("unknown fan-out receipt state")
    store = CardStore(home)
    card = store.fold(request.card_id)
    if card is None:
        raise FanoutBoundaryError("fan-out card is missing")
    request.validate(card, require_unclaimed=False)
    request_rows = [
        row
        for row in store._read_events(request.card_id)
        if row.get("action") == "niobe_fanout_request"
    ]
    if not request_rows or _request_from_event(request.card_id, request_rows[-1]) != request:
        raise FanoutBoundaryError("fan-out receipt request is stale")
    if normalized_state in {
        "claimed",
        "launched",
        "occupied",
        "stopped",
        "released",
        "reassigned",
        "retired",
    }:
        if not claim_owner.strip() or not claim_revision.strip():
            raise FanoutBoundaryError("claim identity is required for runtime receipts")
    if normalized_state in {"claimed", "launched", "occupied", "stopped"} and (
        card.owner != claim_owner or str(card.meta.get("_claim_revision") or "") != claim_revision
    ):
        raise FanoutBoundaryError("fan-out receipt claim generation is stale")
    identity = {
        "request_id": request.request_id,
        "source_head": request.source_head,
        "state": normalized_state,
        "claim_owner": claim_owner,
        "claim_revision": claim_revision,
        "process": dict(process or {}),
    }
    return store.append_event(
        request.card_id,
        "niobe_fanout_receipt",
        "niobe",
        transition_id="niobe-fanout-" + _digest(identity),
        schema="skfleet.niobe-fanout-receipt/v1",
        **identity,
    )


def reconcile_runtime_state(
    *,
    lifecycle: str,
    claim_owner: str | None,
    claim_revision: str | None,
    process_alive: bool,
    prior_owner: str,
    prior_revision: str,
) -> str:
    """Classify current truth for crash recovery and terminal retirement."""

    if lifecycle in {"done", "complete", "archived", "void"}:
        return "retired"
    if claim_owner and claim_revision:
        if (claim_owner, claim_revision) != (prior_owner, prior_revision):
            return "reassigned"
        return "occupied" if process_alive else "stopped"
    return "released"


def reconcile_fanout_receipt(
    home: Path, card_id: str, *, process_alive: bool
) -> dict[str, object] | None:
    """Recover one request's receipt state from fresh claim and process truth."""

    store = CardStore(home)
    rows = store._read_events(card_id)
    requests = [row for row in rows if row.get("action") == "niobe_fanout_request"]
    if not requests:
        return None
    request = _request_from_event(card_id, requests[-1])
    receipts = [
        row
        for row in rows
        if row.get("action") == "niobe_fanout_receipt"
        and row.get("request_id") == request.request_id
        and row.get("source_head") == request.source_head
    ]
    if not receipts:
        return None
    card = store.fold(card_id)
    if card is None:
        raise FanoutBoundaryError("fan-out card is missing")
    request.validate(card, require_unclaimed=False)
    prior = receipts[-1]
    prior_owner = str(prior.get("claim_owner") or "")
    prior_revision = str(prior.get("claim_revision") or "")
    if (prior_owner or prior_revision) and (not prior_owner or not prior_revision):
        raise FanoutBoundaryError("fan-out recovery receipt has incomplete claim identity")
    current_owner = card.owner
    current_revision = str(card.meta.get("_claim_revision") or "") or None
    lifecycle = str(getattr(card.status, "value", card.status)).lower()
    state = reconcile_runtime_state(
        lifecycle=lifecycle,
        claim_owner=current_owner,
        claim_revision=current_revision,
        process_alive=process_alive,
        prior_owner=prior_owner,
        prior_revision=prior_revision,
    )
    receipt_owner = current_owner or prior_owner
    receipt_revision = current_revision or prior_revision
    if not receipt_owner or not receipt_revision:
        return None
    if (
        prior.get("state") == state
        and prior_owner == receipt_owner
        and prior_revision == receipt_revision
    ):
        return None
    return append_fanout_receipt(
        home,
        request,
        state=state,
        claim_owner=receipt_owner,
        claim_revision=receipt_revision,
        process={"alive": process_alive},
    )
