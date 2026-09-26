"""Owner-authorized crew and worker support commands on the existing fleet CLI."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import click
from skcoord.card_store import CardStore

from .herdr_handoff import HandoffManager
from .herdr_handoff_validation import load_json
from .paths import SOVEREIGN_HOME, FleetPaths, default_paths

STATUS_SCHEMA = "skfleet.crew-status/v1"
MAX_STATUS_ROWS = 32
MAX_STATUS_BYTES = 131072
_SEALED_KEYS = {
    "packet",
    "authorizer",
    "parent_contract_sha256",
    "parent_restrictions",
    "source",
    "manifest_sha256",
}
_REQUEST_KEYS = {
    "crew_id",
    "request_id",
    "slot_id",
    "packet",
    "packet_sha256",
    "dedup_key",
    "canonical_request_id",
    "state",
    "submitted_by",
    "helper_packet",
    "consumer_identity",
    "helper_id",
    "cwd",
    "receipt",
    "receipt_sha256",
    "producer_candidate",
}


def _deliveries(paths, home, helper_ids):
    """Query validated receipt copies and retain exact crew custody only."""
    result = {}
    manager = HandoffManager(paths, home, "crew-status", transport=object())
    for row in manager.delivery_records(frozenset(helper_ids)):
        packet = row["packet"]
        helper_id = packet.get("helper_id")
        receipt = row.get("delivery_receipt")
        contract = row.get("contract")
        if (
            receipt.get("helper_id") != helper_id
            or receipt.get("claim_revision") != packet.get("claim_revision")
            or receipt.get("helper_generation") != packet.get("claim_revision")
            or receipt.get("parent_id") != contract.get("parent_id")
        ):
            raise ValueError("delivery receipt identity mismatch")
        if helper_id in result:
            raise ValueError("duplicate helper delivery receipts")
        result[helper_id] = row
    return result


def _status(paths, home, *, parent_id=None, crew_id=None):
    """Build one deterministic read-only crew projection."""
    from . import crew_contract
    from .crew_requests import validate_producer_candidate
    from .crew_store import CrewStore, digest
    from .provider_pairs import validate_provider_pair

    if (parent_id is None) == (crew_id is None):
        raise ValueError("select exactly one parent or crew generation")
    ledger = CrewStore(paths)
    if crew_id is not None:
        crew_contract.identifier(crew_id)
        manifest = ledger.get_manifest(crew_id)
        if manifest is None:
            raise ValueError("crew generation is missing")
    else:
        if not isinstance(parent_id, str) or not re.fullmatch(r"[0-9a-f]{8}", parent_id):
            raise ValueError("invalid parent card ID")
        manifests = [
            row
            for row in ledger.list_manifests()
            if isinstance(row, dict) and row.get("packet", {}).get("parent_id") == parent_id
        ]
        if len(manifests) != 1:
            raise ValueError("parent must resolve to exactly one crew generation")
        manifest = manifests[0]
    if not isinstance(manifest, dict) or set(manifest) != _SEALED_KEYS:
        raise ValueError("invalid sealed crew fields")
    packet = crew_contract.validate_packet(manifest["packet"])
    expected_digest = digest(
        {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    )
    if manifest["manifest_sha256"] != expected_digest:
        raise ValueError("crew manifest digest disagreement")
    parent = CardStore(home).fold(packet["parent_id"])
    if parent is None or parent.meta.get("_claim_revision") != packet["parent_claim_revision"]:
        raise ValueError("parent claim generation mismatch")
    rows = ledger.list_requests(packet["crew_id"])
    if len(rows) > MAX_STATUS_ROWS:
        raise ValueError("crew status row bound exceeded")
    if any(not isinstance(row, dict) or not set(row).issubset(_REQUEST_KEYS) for row in rows):
        raise ValueError("invalid crew request fields")
    slots = {slot["slot_id"]: slot for slot in packet["slots"]}
    canonical = [row for row in rows if row.get("canonical_request_id") == row.get("request_id")]
    helper_ids = [row.get("helper_id") for row in canonical if row.get("helper_id") is not None]
    if len(helper_ids) != len(set(helper_ids)):
        raise ValueError("duplicate helper custody")
    deliveries = _deliveries(paths, home, set(helper_ids))
    helpers = []
    for row in sorted(canonical, key=lambda item: (item["slot_id"], item["request_id"])):
        slot = slots.get(row.get("slot_id"))
        if slot is None:
            raise ValueError("request slot is absent from manifest")
        helper_id = row.get("helper_id")
        if helper_id is None:
            raise ValueError("crew request has incomplete helper assignment")
        helper = CardStore(home).fold(helper_id)
        if helper is None or helper.meta.get("helper_parent_id") != packet["parent_id"]:
            raise ValueError("crew helper custody mismatch")
        generation = helper.meta.get("_claim_revision")
        if not isinstance(generation, str) or not generation:
            raise ValueError("helper generation is missing")
        journal = deliveries.get(helper_id)
        if journal is None:
            raise ValueError("helper delivery receipt is missing")
        receipt = journal["delivery_receipt"]
        binding = journal.get("contract", {}).get("crew_binding")
        if (
            not isinstance(binding, dict)
            or binding.get("crew_id") != packet["crew_id"]
            or binding.get("request_id") != row["request_id"]
            or binding.get("manifest_sha256") != manifest["manifest_sha256"]
            or binding.get("provider_pair") != packet.get("provider_pair")
            or binding.get("producer_candidate") != row.get("packet", {}).get("producer_candidate")
            or receipt["claim_revision"] != generation
        ):
            raise ValueError("delivery receipt differs from sealed crew custody")
        candidate = row.get("producer_candidate")
        if candidate is not None:
            candidate = validate_producer_candidate(candidate)
            if (
                candidate["helper_id"] != helper_id
                or candidate["claim_revision"] != generation
                or candidate["evidence_sha256"] != row.get("receipt_sha256")
            ):
                raise ValueError("producer candidate custody mismatch")
        terminal = receipt["terminal_evidence"]
        helpers.append(
            {
                "request_id": row["request_id"],
                "slot_id": row["slot_id"],
                "role": slot["role"],
                "helper_id": helper_id,
                "helper_owner": helper.owner,
                "helper_generation": generation,
                "logical_route": slot["packet"]["logical_route"],
                "request_state": row["state"],
                "delivery_state": receipt["delivery_state"],
                "host": receipt["host"],
                "workspace_id": receipt["workspace_id"],
                "tab_id": receipt["tab_id"],
                "pane_id": receipt["pane_id"],
                "candidate_custody": candidate,
                "terminal_state": None if terminal is None else terminal["state"],
                "cleanup_eligible": bool(
                    receipt["delivery_state"] == "terminal"
                    and isinstance(terminal, dict)
                    and terminal.get("state") == "done"
                    and terminal.get("process_group_alive") is False
                ),
            }
        )
    pair = packet.get("provider_pair")
    if pair is not None:
        pair = validate_provider_pair(pair)
    result = {
        "schema": STATUS_SCHEMA,
        "crew_id": packet["crew_id"],
        "parent_id": packet["parent_id"],
        "parent_owner": parent.owner,
        "parent_claim_revision": packet["parent_claim_revision"],
        "manifest_sha256": manifest["manifest_sha256"],
        "provider_pair": pair,
        "review_availability": (
            "unconfigured"
            if pair is None
            else "available" if pair["state"] == "paired" else "independent_review_unavailable"
        ),
        "helpers": helpers,
    }
    if len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()) > MAX_STATUS_BYTES:
        raise ValueError("crew status output bound exceeded")
    return result


def _human_status(result):
    lines = [
        f"crew {result['crew_id']} parent {result['parent_id']} "
        f"generation {result['parent_claim_revision']}",
        f"manifest {result['manifest_sha256']} review {result['review_availability']}",
    ]
    lines.extend(
        f"{row['slot_id']} {row['role']} helper={row['helper_id']} "
        f"owner={row['helper_owner']} generation={row['helper_generation']} "
        f"route={row['logical_route']} host={row['host']} workspace={row['workspace_id']} "
        f"tab={row['tab_id']} pane={row['pane_id']} "
        f"request={row['request_state']} delivery={row['delivery_state']} "
        f"terminal={row['terminal_state'] or '-'} cleanup={str(row['cleanup_eligible']).lower()}"
        for row in result["helpers"]
    )
    return "\n".join(lines)


def _roots(home):
    """Keep an explicit test home from accidentally addressing the live fleet."""
    if home is not None:
        root = home.expanduser().absolute()
        return FleetPaths(root / "fleet"), root
    return default_paths(), Path(os.environ.get("SKCAPSTONE_HOME") or SOVEREIGN_HOME).expanduser()


def _options(command):
    """Require the actual acting principal and explicit bounded packet."""
    command = click.option("--agent", required=True)(command)
    command = click.option(
        "--packet", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path)
    )(command)
    return click.option("--home", type=click.Path(file_okay=False, path_type=Path))(command)


def register_crew_commands(fleet: click.Group) -> None:
    """Register control/intake commands without starting any dispatcher."""

    @fleet.group("crew")
    def crew():
        """Register bounded crews, ask for support, and inspect exact deliveries."""

    def invoke(operation, home, agent, packet):
        try:
            paths, root = _roots(home)
            result = operation(paths, root, agent, load_json(packet))
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True))

    @crew.command("register")
    @_options
    def register_cmd(home, agent, packet):
        """Persist an exact parent-owner mandate; create no helper or worker yet."""
        from .crew_requests import register

        invoke(register, home, agent, packet)

    @crew.command("request")
    @_options
    def request_cmd(home, agent, packet):
        """Ask the crew for one authorized support slot, coalescing equivalent work."""
        from .crew_requests import submit_support

        invoke(submit_support, home, agent, packet)

    @crew.command("receipt")
    @_options
    def receipt_cmd(home, agent, packet):
        """Record verified artifact custody or a specific blocker, not acceptance."""
        from .crew_requests import record_receipt

        invoke(record_receipt, home, agent, packet)

    @crew.command("status")
    @click.argument("legacy_crew_id", required=False)
    @click.option("--parent", "parent_id")
    @click.option("--crew", "crew_id")
    @click.option("--json", "as_json", is_flag=True)
    @click.option("--home", type=click.Path(file_okay=False, path_type=Path))
    def status_cmd(legacy_crew_id, parent_id, crew_id, as_json, home):
        """Read one exact crew generation without dispatch or reconciliation."""
        try:
            if legacy_crew_id is not None:
                if crew_id is not None:
                    raise ValueError("crew generation was selected more than once")
                crew_id = legacy_crew_id
                as_json = True
            paths, root = _roots(home)
            result = _status(paths, root, parent_id=parent_id, crew_id=crew_id)
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(result, sort_keys=True) if as_json else _human_status(result))

    @crew.command("reconcile")
    @click.option("--home", type=click.Path(file_okay=False, path_type=Path))
    @click.option("--limit", default=8, type=click.IntRange(1, 8))
    def reconcile_cmd(home, limit):
        """Consume existing mandates once on this host, honoring fleet pause."""
        from .crew_controller import reconcile_crews

        try:
            paths, root = _roots(home)
            report = reconcile_crews(
                paths, root, socket.gethostname().strip().lower(), limit=limit
            )
        except (ValueError, RuntimeError, OSError) as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(report, sort_keys=True))
