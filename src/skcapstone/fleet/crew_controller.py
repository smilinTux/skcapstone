"""Reconcile explicitly authorized crews on one host without replacing owners."""

from __future__ import annotations

import hashlib
import socket
from datetime import datetime, timezone
from pathlib import Path

from skcoord.card_store import CardStore

from ..coord_helpers import request_help
from . import store as fleet_store
from .crew_contract import helper_packet, validate_mandate
from .crew_store import CrewStore, identifier
from .paths import FleetPaths

MAX_PER_CYCLE = 8
MAX_CONSIDERED = 32


def _next_manifest(paths, ledger, manifests, considered):
    """Persist fair local scan progress without holding a lock during intake."""
    remaining = [row for row in manifests if row["packet"]["crew_id"] not in considered]
    with ledger.lock(), fleet_store.actuation_exclusion(paths):
        gate = fleet_store.check_actuation_gate(paths)
        if not gate.allowed:
            return None, gate.reason
        cursor = ledger._read("controller-cursor.json")
        last = ""
        if cursor is not None:
            if set(cursor) != {"schema", "last_considered"} or cursor["schema"] != 1:
                raise ValueError("invalid crew controller cursor")
            last = identifier(cursor["last_considered"])
        manifest = next(
            (row for row in remaining if row["packet"]["crew_id"] > last), remaining[0]
        )
        advanced = {"schema": 1, "last_considered": manifest["packet"]["crew_id"]}
        if cursor != advanced:
            ledger._write("controller-cursor.json", advanced)
        return manifest, None


def _auto_requests(paths, home, manifest, now):
    """Activate bounded initial work or one diagnostic per progress observation."""
    from .crew_requests import submit_support

    packet = manifest["packet"]
    for slot in packet["slots"]:
        trigger = slot["trigger"]
        if trigger == "support-request":
            continue
        evidence = manifest["manifest_sha256"]
        if trigger == "stale-progress":
            from .crew_progress import stalled_evidence

            evidence = stalled_evidence(paths, manifest, now)
            if evidence is None:
                continue
        identity = hashlib.sha256(f"{slot['slot_id']}:{evidence}".encode()).hexdigest()
        submit_support(
            paths,
            home,
            manifest["authorizer"],
            {
                "schema": "skfleet.crew-support/v1",
                "crew_id": packet["crew_id"],
                "request_id": f"auto-{identity[:40]}",
                "slot_id": slot["slot_id"],
                "requester_card_id": packet["parent_id"],
                "requester_claim_revision": packet["parent_claim_revision"],
                "evidence_sha256": evidence,
                "reason": f"Authorized {trigger} crew assignment",
            },
            automatic=True,
        )


def _counts(cards, rows):
    """Count reserved and unresolved helper work, including uncertain creation."""
    active = total = 0
    for row in rows:
        if row.get("canonical_request_id") != row["request_id"]:
            continue
        if row["state"] not in {"creating", "assigned", "delivered", "blocked"}:
            continue
        if not row.get("helper_id"):
            if row["state"] == "creating":
                active += 1
                total += 1
            continue
        total += 1
        child = cards.fold(row["helper_id"])
        # Missing or unreadable status is occupied custody, never free capacity.
        if child is None or child.status.value != "done":
            active += 1
    return active, total


def _dependencies_done(cards, parent_id):
    """Require authoritative dependency completion without deleting an edge."""
    parent = cards.fold(parent_id)
    if parent is None:
        return False
    for dep_id in parent.dependencies:
        dep = cards.fold(dep_id)
        if dep is None or dep.status.value != "done" or dep.meta.get("voided"):
            return False
    return True


def _consume(paths, home, node, manifest, ledger, remaining):
    """Persist creation intent, then replay the existing idempotent helper API."""
    created = []
    with ledger.lock(), fleet_store.actuation_exclusion(paths):
        if not fleet_store.check_actuation_gate(paths).allowed:
            return created, "frozen"
        validate_mandate(home, manifest)
        packet = manifest["packet"]
        cards = CardStore(home)
        if not _dependencies_done(cards, packet["parent_id"]):
            return created, "dependencies"
        rows = ledger.list_requests(packet["crew_id"])
        active, total = _counts(cards, rows)
        slots = {slot["slot_id"]: slot for slot in packet["slots"]}
        for row in rows:
            if len(created) >= remaining:
                break
            if row.get("canonical_request_id") != row["request_id"]:
                continue
            if row["state"] not in {"requested", "creating"}:
                continue
            replay = row["state"] == "creating"
            if not replay and (
                active >= packet["max_active_helpers"] or total >= packet["max_helpers"]
            ):
                continue
            slot = slots[row["slot_id"]]
            child_packet = helper_packet(manifest, row, slot)
            if replay and row.get("helper_packet") != child_packet:
                raise ValueError("creation intent conflicts with authorized helper packet")
            if not replay:
                row.update(
                    state="creating",
                    helper_packet=child_packet,
                    consumer_identity={"node": node, "role": "sknoded-crew"},
                )
                ledger.save_request(row)
            # Board.create_helper_task revalidates parent under its own locks.
            report = request_help(
                home,
                packet["parent_id"],
                manifest["authorizer"],
                packet["parent_claim_revision"],
                child_packet,
                expected_parent={
                    "contract_sha256": manifest["parent_contract_sha256"],
                    "source": manifest["source"],
                    "labels": manifest["parent_restrictions"]["labels"],
                    "dependencies": manifest["parent_restrictions"]["dependencies"],
                },
            )
            row.update(state="assigned", helper_id=report["helper_id"])
            ledger.save_request(row)
            created.append(report["helper_id"])
            if not replay:
                active += 1
                total += 1
        pending = any(row["state"] in {"requested", "creating"} for row in rows)
        return created, "capacity-or-budget" if pending and not created else "reconciled"


def reconcile_crews(
    paths: FleetPaths,
    home: Path,
    node: str,
    *,
    limit: int = MAX_PER_CYCLE,
    now: datetime | None = None,
) -> dict | None:
    """Consume opt-in mandates from the existing daemon; never launch processes.

    Normal fleet dispatch owns placement, claim and launch. No manifest means no
    store writes or models. A pause prevents automatic intake and helper creation.
    One bad crew is reported without preventing the node heartbeat or other crews.
    """
    if node != socket.gethostname().strip().lower():
        return None
    if type(limit) is not int or not 1 <= limit <= MAX_PER_CYCLE:
        raise ValueError("cycle limit must be between 1 and 8")
    ledger = CrewStore(paths)
    manifests, errors = ledger.scan_manifests()
    if not manifests and not errors:
        return None
    gate = fleet_store.check_actuation_gate(paths)
    if not gate.allowed:
        return {"state": gate.reason, "created": [], "crews": []}
    current = now or datetime.now(timezone.utc)
    report = {"state": "reconciled", "created": [], "crews": errors}
    considered = set()
    for _ in range(min(MAX_CONSIDERED, len(manifests))):
        if len(report["created"]) >= limit:
            break
        manifest, stopped = _next_manifest(paths, ledger, manifests, considered)
        if stopped is not None:
            report["state"] = stopped
            break
        considered.add(manifest["packet"]["crew_id"])
        crew_id = "invalid-manifest"
        try:
            validate_mandate(home, manifest)
            packet = manifest["packet"]
            crew_id = packet["crew_id"]
            if packet["coordinator_node"] != node:
                continue
            _auto_requests(paths, home, manifest, current)
            created, state = _consume(
                paths, home, node, manifest, ledger, limit - len(report["created"])
            )
            report["created"].extend(created)
            result = {"crew_id": packet["crew_id"], "state": state}
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            result = {
                "crew_id": crew_id,
                "state": "held",
                "reason": str(exc)[:512],
            }
        report["crews"].append(result)
    return report
