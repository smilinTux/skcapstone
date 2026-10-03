"""Owner-authorized support intake and verified delivery, never acceptance."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import nullcontext
from pathlib import Path, PurePosixPath

from skcoord.card_store import CardStore, card_mutation_lock

from . import crew_contract
from . import store as fleet_store
from .crew_admission import validate_helper_generation
from .crew_store import CrewStore, digest
from .herdr_handoff_validation import _git, _regular, _source, _text

SUPPORT_SCHEMA = "skfleet.crew-support/v1"
RECEIPT_SCHEMA = "skfleet.crew-receipt/v1"
_REQUEST = {
    "schema",
    "request_id",
    "crew_id",
    "slot_id",
    "requester_card_id",
    "requester_claim_revision",
    "evidence_sha256",
    "reason",
}
_RECEIPT = {"schema", "crew_id", "request_id", "helper_id", "claim_revision", "cwd"}


def _copy(value):
    """Copy a finite bounded JSON packet, preserving no caller-owned aliases."""
    if not isinstance(value, dict):
        raise ValueError("crew packet must be an object")
    data = json.dumps(value, allow_nan=False)
    if len(data.encode()) > 32768:
        raise ValueError("crew packet exceeds bounded input")
    return json.loads(data)


def _sha(value):
    """Require an exact lowercase SHA-256 string."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("invalid evidence SHA-256")
    return value


def _card_id(value):
    """Require an exact card ID rather than a path or prefix."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{8}", value):
        raise ValueError("invalid requester/helper card ID")
    return value


def _manifest(store, home, crew_id):
    """Read and freshly validate a mandate on its designated host."""
    record = store.get_manifest(crew_id)
    if record is None:
        raise ValueError("crew manifest is missing")
    record = crew_contract.validate_mandate(Path(home), record)
    if record["packet"]["coordinator_node"] != store.node:
        raise ValueError("wrong crew coordinator host")
    return record


def _slot(record, slot_id):
    """Select a pre-authorized slot without accepting new instructions."""
    for slot in record["packet"]["slots"]:
        if slot["slot_id"] == slot_id:
            return slot
    raise ValueError("crew slot is not authorized")


def _claim(home, actor, card_id, revision):
    """Read an exact active claimant while its local card lock is held."""
    card = CardStore(Path(home)).fold(card_id)
    if (
        card is None
        or card.owner != actor
        or card.meta.get("_claim_revision") != revision
        or card.archived
        or card.meta.get("voided")
        or card.meta.get("claim_conflicts")
        or card.status.value not in {"ready", "doing"}
        or {str(label).lower() for label in card.labels}
        & {"do-not-claim", "integration_hold", "integration-hold"}
    ):
        raise ValueError("current exact active claimant required")
    return card


def _requester(store, home, actor, packet, manifest):
    """Admit the current parent or a currently claimed helper from this crew."""
    parent = manifest["packet"]
    with card_mutation_lock(Path(home), packet["requester_card_id"]):
        card = _claim(home, actor, packet["requester_card_id"], packet["requester_claim_revision"])
        if card.id == parent["parent_id"]:
            if (
                actor != manifest["authorizer"]
                or packet["requester_claim_revision"] != parent["parent_claim_revision"]
            ):
                raise ValueError("request does not match crew parent claim")
            return
        helpers = {row.get("helper_id") for row in store.list_requests(parent["crew_id"])}
        if card.id not in helpers or (
            card.meta.get("helper_parent_id") != parent["parent_id"]
            or card.meta.get("helper_parent_claim_revision") != parent["parent_claim_revision"]
            or card.meta.get("helper_parent_contract_sha256") != manifest["parent_contract_sha256"]
            or _source(card) != manifest["source"]
        ):
            raise ValueError("requester is not a current helper of this crew")
        validate_helper_generation(Path(home), card)


def register(paths, home, actor, packet):
    """Persist a contract-authorized immutable crew without creating cards."""
    with CrewStore(paths).lock() as store:
        record = crew_contract.register_payload(Path(home), actor, packet)
        if record["packet"]["coordinator_node"] != store.node:
            raise ValueError("wrong crew coordinator host")
        current = record["packet"]
        current_paths = _write_scopes(current)
        for existing in store.list_manifests():
            other = crew_contract.validate_packet(existing["packet"])
            if other["crew_id"] == current["crew_id"] or (
                other["parent_id"],
                other["parent_claim_revision"],
            ) != (current["parent_id"], current["parent_claim_revision"]):
                continue
            if any(
                left == right or left in right.parents or right in left.parents
                for left in current_paths
                for right in _write_scopes(other)
            ):
                raise ValueError("cross-manifest crew write scopes overlap")
        return store.put_manifest(record)


def _write_scopes(packet):
    """Return every reserved owner and helper write path in one mandate."""
    return [
        PurePosixPath(path)
        for path in packet["owner_paths"]
        + [path for slot in packet["slots"] for path in slot["packet"]["allowed_paths"]]
    ]


def _support_packet(packet):
    """Validate strict support intake that selects an existing slot."""
    packet = _copy(packet)
    if set(packet) != _REQUEST or packet["schema"] != SUPPORT_SCHEMA:
        raise ValueError("invalid support request fields or schema")
    for key in ("request_id", "crew_id", "slot_id"):
        crew_contract.identifier(packet[key])
    _card_id(packet["requester_card_id"])
    _text(packet["requester_claim_revision"], "requester claim revision", 128)
    _sha(packet["evidence_sha256"])
    _text(packet["reason"], "request reason", 2048)
    return packet


def _dedup(manifest, slot, packet):
    """Consolidate equivalent resources only inside the exact source mandate."""
    common = {
        "crew_id": packet["crew_id"],
        "slot_id": slot["slot_id"],
        "parent_id": manifest["packet"]["parent_id"],
        "parent_claim_revision": manifest["packet"]["parent_claim_revision"],
        "manifest_sha256": manifest["manifest_sha256"],
        "source": manifest["source"],
    }
    if "resource" in slot:
        common["resource"] = slot["resource"]
    else:
        common.update(slot_id=slot["slot_id"], evidence_sha256=packet["evidence_sha256"])
    return digest(common)


def submit_support(paths, home, actor, packet, *, automatic=False):
    """Queue useful work immediately; never create a card or call a model."""
    packet = _support_packet(packet)
    with (
        CrewStore(paths).lock() as store,
        fleet_store.actuation_exclusion(paths) if automatic else nullcontext(),
    ):
        if automatic and not fleet_store.check_actuation_gate(paths).allowed:
            raise ValueError("automatic crew intake stopped by fleet freeze")
        manifest = _manifest(store, home, packet["crew_id"])
        slot = _slot(manifest, packet["slot_id"])
        _requester(store, home, actor, packet, manifest)
        # Only the original owner may select controller-driven initial/stall slots.
        if (
            slot["trigger"] != "support-request"
            and packet["requester_card_id"] != manifest["packet"]["parent_id"]
        ):
            raise ValueError("helper requests require a support-request slot")
        previous = store.get_request(packet["crew_id"], packet["request_id"])
        packet_hash = digest(packet)
        if previous is not None:
            if previous["packet_sha256"] != packet_hash or previous["packet"] != packet:
                raise ValueError("support request ID replay conflict")
            return previous
        key = _dedup(manifest, slot, packet)
        canonical = next(
            (
                row
                for row in store.list_requests(packet["crew_id"])
                if row["dedup_key"] == key and row["canonical_request_id"] == row["request_id"]
            ),
            None,
        )
        row = {
            "crew_id": packet["crew_id"],
            "request_id": packet["request_id"],
            "slot_id": packet["slot_id"],
            "state": "alias" if canonical else "requested",
            "packet": packet,
            "packet_sha256": packet_hash,
            "dedup_key": key,
            "canonical_request_id": canonical["request_id"] if canonical else packet["request_id"],
            "submitted_by": actor,
        }
        return store.save_request(row)


def status(paths, crew_id):
    """Read current delivery records without making a missing store exist."""
    crew_contract.identifier(crew_id)
    store = CrewStore(paths)
    if not store.directory.exists():
        return {"crew_id": crew_id, "manifest": None, "requests": []}
    with store.lock():
        return {
            "crew_id": crew_id,
            "manifest": store.get_manifest(crew_id),
            "requests": store.list_requests(crew_id),
        }


def _workspace(payload, manifest):
    """Verify receipt workspace identity, source remote and pinned ancestry."""
    cwd = Path(_text(payload["cwd"], "receipt cwd"))
    if not cwd.is_absolute() or cwd.resolve(strict=True) != cwd or not cwd.is_dir():
        raise ValueError("receipt cwd must be an exact canonical directory")
    source = manifest["source"]
    if (
        Path(_git(cwd, "rev-parse", "--show-toplevel")) != cwd
        or _git(cwd, "remote", "get-url", "origin") != source["repository"]
    ):
        raise ValueError("receipt workspace source mismatch")
    _git(cwd, "merge-base", "--is-ancestor", source["base_revision"], "HEAD")
    return cwd


def _result(payload, cwd):
    """Hash bounded regular artifacts and validate reported, unexecuted tests."""
    if "blocker" in payload:
        _text(payload["blocker"], "explicit blocker", 4096)
        return
    artifacts, tests = payload["artifacts"], payload["tests"]
    if (
        not isinstance(artifacts, list)
        or not 1 <= len(artifacts) <= 16
        or not isinstance(tests, list)
        or not 1 <= len(tests) <= 16
    ):
        raise ValueError("bounded artifacts and reported tests required")
    seen = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise ValueError("invalid result artifact fields")
        name = _text(artifact["path"], "artifact path", 1024)
        relative = Path(name)
        if (
            relative.is_absolute()
            or str(relative) != name
            or name in seen
            or any(part in {"..", ".git"} for part in relative.parts)
        ):
            raise ValueError("artifact must be unique and inside the workspace")
        seen.add(name)
        data = _regular(cwd / relative, 16 * 1024 * 1024)
        if hashlib.sha256(data).hexdigest() != _sha(artifact["sha256"]):
            raise ValueError("artifact bytes do not match receipt")
    for test in tests:
        if not isinstance(test, dict) or set(test) != {"command", "outcome"}:
            raise ValueError("invalid reported test fields")
        _text(test["command"], "reported command", 2048)
        _text(test["outcome"], "reported outcome", 2048)


def record_receipt(paths, home, actor, packet):
    """Record verified helper delivery or an exact blocker, never acceptance."""
    payload = _copy(packet)
    if (
        set(payload) not in (_RECEIPT | {"artifacts", "tests"}, _RECEIPT | {"blocker"})
        or payload["schema"] != RECEIPT_SCHEMA
    ):
        raise ValueError("invalid crew receipt fields or schema")
    for key in ("crew_id", "request_id"):
        crew_contract.identifier(payload[key])
    _card_id(payload["helper_id"])
    _text(payload["claim_revision"], "helper claim revision", 128)
    with CrewStore(paths).lock() as store:
        manifest = _manifest(store, home, payload["crew_id"])
        row = store.get_request(payload["crew_id"], payload["request_id"])
        if (
            row is None
            or row["canonical_request_id"] != row["request_id"]
            or row.get("helper_id") != payload["helper_id"]
        ):
            raise ValueError("receipt does not match assigned canonical helper")
        with card_mutation_lock(Path(home), payload["helper_id"]):
            helper = _claim(home, actor, payload["helper_id"], payload["claim_revision"])
            validate_helper_generation(Path(home), helper)
            if (
                helper.meta.get("helper_parent_id") != manifest["packet"]["parent_id"]
                or helper.meta.get("helper_parent_claim_revision")
                != manifest["packet"]["parent_claim_revision"]
                or helper.meta.get("helper_parent_contract_sha256")
                != manifest["parent_contract_sha256"]
                or _source(helper) != manifest["source"]
            ):
                raise ValueError("receipt helper mandate changed")
            cwd = _workspace(payload, manifest)
            if row.get("cwd") is not None and row["cwd"] != str(cwd):
                raise ValueError("receipt workspace custody changed")
            _result(payload, cwd)
            if "receipt" in row and row["receipt"] != payload:
                raise ValueError("receipt replay conflict")
            row.update(
                receipt=payload,
                receipt_sha256=digest(payload),
                state="blocked" if "blocker" in payload else "delivered",
            )
            return store.save_request(row)
