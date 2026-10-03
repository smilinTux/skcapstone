"""Bounded original-owner authorization for persistent helper crews."""

from __future__ import annotations

import hashlib
import json
import re
import socket
from pathlib import Path, PurePosixPath

from skcoord.card_store import CardStore, card_mutation_lock, explicit_creation_request_digest
from skcoord.owner_helpers import parent_contract_digest

from ..coord_helpers import _source, build_helper
from ..coord_helpers import validate_packet as validate_helper_packet
from ..jarvis_emergency import authorize_coord_mutation
from ..seat_boundaries import Action

SCHEMA = "skfleet.crew/v1"
MAX_MANIFEST_BYTES = 131072
_KEYS = {
    "schema",
    "crew_id",
    "parent_id",
    "parent_claim_revision",
    "coordinator_node",
    "owner_paths",
    "max_active_helpers",
    "max_helpers",
    "slots",
}
_SEALED = {
    "packet",
    "authorizer",
    "parent_contract_sha256",
    "parent_restrictions",
    "source",
    "manifest_sha256",
}


def identifier(value: object) -> str:
    """Require a bounded path-safe crew, node, slot or request identifier."""
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", value)
        or ".." in value
    ):
        raise ValueError("invalid crew identifier")
    return value


def _bounded(value: object) -> dict:
    """Copy bounded JSON without accepting non-finite values or object aliases."""
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("crew manifest exceeds bounded input")
    return json.loads(encoded)


def _paths(values: object) -> list[str]:
    """Validate literal owner scope with the same helper packet path rules."""
    return validate_helper_packet(
        {
            "request_id": "scope-check",
            "title": "[S] Scope",
            "objective": "Validate scope",
            "criteria": ["Literal scope"],
            "allowed_paths": values,
            "verification_commands": ["No execution"],
        }
    )["allowed_paths"]


def validate_packet(packet: object) -> dict:
    """Validate the exact finite crew declaration, without deriving new work."""
    if not isinstance(packet, dict) or set(packet) != _KEYS or packet["schema"] != SCHEMA:
        raise ValueError("invalid crew manifest fields or schema")
    packet = _bounded(packet)
    for key in ("crew_id", "coordinator_node"):
        identifier(packet[key])
    if not isinstance(packet["parent_id"], str) or not re.fullmatch(
        r"[0-9a-f]{8}", packet["parent_id"]
    ):
        raise ValueError("invalid crew parent ID")
    revision = packet["parent_claim_revision"]
    if (
        not isinstance(revision, str)
        or not revision
        or len(revision) > 128
        or any(char.isspace() or ord(char) < 32 for char in revision)
    ):
        raise ValueError("invalid parent claim revision")
    for key, maximum in (("max_active_helpers", 8), ("max_helpers", 32)):
        if type(packet[key]) is not int or not 1 <= packet[key] <= maximum:
            raise ValueError(f"invalid {key}")
    if packet["max_active_helpers"] > packet["max_helpers"]:
        raise ValueError("active allowance exceeds total helper allowance")
    packet["owner_paths"] = _paths(packet["owner_paths"])
    slots = packet["slots"]
    if not isinstance(slots, list) or not 1 <= len(slots) <= 16:
        raise ValueError("crew requires 1 to 16 slots")
    seen_slots, seen_requests = set(), set()
    scopes = [PurePosixPath(path) for path in packet["owner_paths"]]
    for slot in slots:
        required = {"slot_id", "role", "trigger", "packet"}
        if not isinstance(slot, dict) or set(slot) not in (required, required | {"resource"}):
            raise ValueError("invalid crew slot fields")
        identifier(slot["slot_id"])
        if slot["slot_id"] in seen_slots:
            raise ValueError("duplicate crew slot")
        seen_slots.add(slot["slot_id"])
        if not isinstance(slot["role"], str) or slot["role"] not in {
            "dispatch",
            "technical",
            "verification",
            "support",
        }:
            raise ValueError("invalid crew role")
        if not isinstance(slot["trigger"], str) or slot["trigger"] not in {
            "initial",
            "support-request",
            "stale-progress",
        }:
            raise ValueError("invalid crew trigger")
        slot["packet"] = validate_helper_packet(slot["packet"])
        if slot["packet"]["request_id"] in seen_requests:
            raise ValueError("duplicate helper template request ID")
        seen_requests.add(slot["packet"]["request_id"])
        paths = [PurePosixPath(path) for path in slot["packet"]["allowed_paths"]]
        if slot["trigger"] == "stale-progress" and paths:
            raise ValueError("automatic stale-progress diagnostics must be read-only")
        if any(a == b or a in b.parents or b in a.parents for a in scopes for b in paths):
            raise ValueError("crew owner or helper write scopes overlap")
        scopes.extend(paths)
        if "resource" in slot:
            resource = slot["resource"]
            if not isinstance(resource, dict) or set(resource) != {
                "resource_id",
                "version",
                "policy_sha256",
                "context_sha256",
            }:
                raise ValueError("invalid support resource fields")
            identifier(resource["resource_id"])
            if (
                not isinstance(resource["version"], str)
                or not resource["version"]
                or len(resource["version"]) > 128
                or any(c.isspace() or ord(c) < 32 for c in resource["version"])
            ):
                raise ValueError("invalid support resource version")
            for key in ("policy_sha256", "context_sha256"):
                if not isinstance(resource[key], str) or not re.fullmatch(
                    r"[0-9a-f]{64}", resource[key]
                ):
                    raise ValueError("invalid support policy/context digest")
    return packet


def _seal(home: Path, actor: str, packet: dict) -> dict:
    """Snapshot one authorized parent generation under its existing mutation lock."""
    if not isinstance(actor, str) or not actor or len(actor) > 128:
        raise ValueError("invalid crew authorizer")
    with card_mutation_lock(home, packet["parent_id"]):
        parent = CardStore(home).fold(packet["parent_id"])
        if parent is None:
            raise ValueError("crew parent is missing")
        authorize_coord_mutation(actor, Action.CREATE_CARD, parent.id, None, None)
        for slot in packet["slots"]:
            build_helper(parent, slot["packet"], actor, packet["parent_claim_revision"])
        record = {
            "packet": packet,
            "authorizer": actor,
            "parent_contract_sha256": parent_contract_digest(parent),
            "parent_restrictions": {
                "labels": sorted(parent.labels),
                "dependencies": sorted(parent.dependencies),
            },
            "source": _source(parent),
        }
        record["manifest_sha256"] = explicit_creation_request_digest(record)
        return _bounded(record)


def helper_packet(manifest, row, slot):
    """Forward only sealed scope/resource fields and exact delivery correlation."""
    packet = dict(slot["packet"])
    identity = hashlib.sha256(
        f"{manifest['manifest_sha256']}:{row['dedup_key']}".encode()
    ).hexdigest()
    packet["request_id"] = f"crew-{identity[:40]}"
    correlation = {
        "crew_id": row["crew_id"],
        "request_id": row["request_id"],
        "coordinator_node": manifest["packet"]["coordinator_node"],
    }
    instruction = (
        " Crew delivery: "
        + json.dumps(correlation, sort_keys=True)
        + ". Return a skfleet.crew-receipt/v1 packet with these crew/request IDs, "
        "your helper_id, exact claim_revision, canonical cwd and artifacts [{path,sha256}] "
        "plus tests [{command,outcome}], or an explicit blocker. On the coordinator host "
        "use skfleet crew receipt --agent YOUR_CURRENT_OWNER --packet RECEIPT.json. "
        "If remote, return custody through the existing authorized evidence route; "
        "do not assume a local ledger write reaches the coordinator. Receipt is delivery, "
        "not parent acceptance."
    )
    if "resource" in slot:
        instruction += (
            " Support resource: "
            + json.dumps(slot["resource"], sort_keys=True)
            + ". Reuse matching already-authorized resources first. If absent, construct "
            "only what this exact source scope authorizes, otherwise report the missing "
            "capability. Do not install, execute downloaded tools or widen access."
        )
    packet["objective"] += instruction
    return validate_helper_packet(packet)


def register_payload(home: Path, actor: str, packet: object) -> dict:
    """Authorize and seal an exact local crew; no persistence or card mutation."""
    packet = validate_packet(packet)
    if packet["coordinator_node"] != socket.gethostname().strip().lower():
        raise ValueError("crew registration requires its actual coordinator host")
    record = _seal(Path(home), actor, packet)
    for slot in packet["slots"]:
        helper_packet(
            record,
            {"crew_id": packet["crew_id"], "request_id": "r" * 64, "dedup_key": "0" * 64},
            slot,
        )
    return record


def validate_mandate(home: Path, record: object) -> dict:
    """Recheck original capability and the complete current parent generation."""
    if not isinstance(record, dict) or set(record) != _SEALED:
        raise ValueError("invalid sealed crew fields")
    record = _bounded(record)
    packet = validate_packet(record["packet"])
    expected = _seal(Path(home), record["authorizer"], packet)
    if record != expected:
        raise ValueError("crew mandate digest or parent authority changed")
    return expected
