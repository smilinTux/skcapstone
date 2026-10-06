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
PRESTART_SCHEMA = "skfleet.remote-review-prestart-retirement/v1"
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
        if (
            event.get("action") == "remote_review_prestart_retire"
            and event.get("schema") == PRESTART_SCHEMA
        ):
            request_id = event.get("request_id")
            if not isinstance(request_id, str) or not SHA.fullmatch(request_id):
                continue
            receipt = _receipt_path(home, card, request_id)
            try:
                raw_receipt = _raw(receipt)
                value = json.loads(raw_receipt)
                archived = _raw(receipt.parent / "request.json")
                archived_request = json.loads(archived)
                proof = _raw(receipt.parent / "prestart.json")
                proof_value = json.loads(proof)
            except (OSError, ValueError):
                continue
            offers = [
                row
                for row in events
                if row.get("action") == "remote_review_offer"
                and row.get("request_id") == request_id
                and row.get("request_sha256") == event.get("offer_request_sha256")
            ]
            releases = [
                row
                for row in events
                if row.get("action") == "release_claim"
                and row.get("writer") == event.get("actor")
                and row.get("released_owner") == event.get("previous_owner")
                and row.get("expected_claim_revision") == event.get("previous_claim_revision")
            ]
            expected_unit = (
                "skfleet-worker-"
                + str(archived_request.get("production", {}).get("family", ""))
                + "-"
                + card
                + ".service"
            )
            if (
                len(offers) == 1
                and len(releases) == 1
                and event.get("writer") == event.get("actor")
                and dispatch.valid_name(event.get("actor", ""))
                and value.get("schema") == PRESTART_SCHEMA
                and value.get("card_id") == card
                and value.get("request_id") == request_id
                and value.get("request_sha256") == event.get("request_sha256")
                and value.get("previous_owner") == event.get("previous_owner")
                and value.get("previous_claim_revision") == event.get("previous_claim_revision")
                and source_bundle._sha(archived) == event.get("request_sha256")
                and production_builder.digest(archived_request)
                == event.get("offer_request_sha256")
                and archived_request.get("reviewer") == event.get("previous_owner")
                and proof_value.get("unit") == expected_unit
                and proof_value.get("unit_state", {}).get("LoadState") == "not-found"
                and proof_value.get("matching_sessions") == 0
                and proof_value.get("dispatch_status_absent") is True
                and SHA.fullmatch(proof_value.get("admission_inventory_sha256", ""))
                and value.get("prestart_sha256") == event.get("prestart_sha256")
                and source_bundle._sha(proof) == event.get("prestart_sha256")
                and source_bundle._sha(raw_receipt) == event.get("receipt_sha256")
            ):
                retired.add(request_id)
            continue
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
        except (OSError, ValueError):
            continue
        exit_source = value.get("exit_source", "worker-exit")
        try:
            if exit_source == "worker-exit":
                archived_exit = _raw(receipt.parent / "worker-exit.json")
                exit_value = json.loads(archived_exit)
                status_value = json.loads(archived_status)
                exit_proven = (
                    exit_value.get("card_id") == card
                    and exit_value.get("owner") == status_value.get("owner")
                    and exit_value.get("claim_revision") == status_value.get("claim_revision")
                    and exit_value.get("host") == status_value.get("execution", {}).get("host")
                    and type(exit_value.get("child_exit_code")) is int
                    and exit_value.get("child_exit_code") != 0
                )
            elif exit_source == "systemd-terminal":
                archived_exit = _raw(receipt.parent / "systemd-terminal.json")
                exit_value = json.loads(archived_exit)
                status_value = json.loads(archived_status)
                execution = status_value.get("execution") or {}
                expected_unit = (
                    "skfleet-worker-"
                    + str(archived_request.get("production", {}).get("family", ""))
                    + "-"
                    + card
                    + ".service"
                )
                journal = exit_value.get("journal")
                if journal is None:
                    exit_proven = (
                        exit_value.get("InvocationID") == execution.get("invocation")
                        and exit_value.get("InvocationID") == value.get("invocation")
                        and value.get("unit") == expected_unit
                        and exit_value.get("LoadState") == "loaded"
                        and exit_value.get("ActiveState") == "failed"
                        and exit_value.get("SubState") == "failed"
                        and exit_value.get("MainPID") == "0"
                        and exit_value.get("ControlPID") == "0"
                        and exit_value.get("Result") not in {None, "", "success"}
                        and exit_value.get("ExecMainStatus") not in {None, "", "0"}
                    )
                else:
                    absence = exit_value.get("absence") or {}
                    exit_proven = (
                        value.get("unit") == expected_unit
                        and exit_value.get("invocation") == execution.get("invocation")
                        and journal.get("schema") == "skfleet.collected-worker-terminal/v1"
                        and journal.get("unit") == expected_unit
                        and journal.get("host") == execution.get("host")
                        and journal.get("card_id") == card
                        and journal.get("owner") == status_value.get("owner")
                        and journal.get("claim_revision") == status_value.get("claim_revision")
                        and journal.get("request_id") == request_id
                        and journal.get("started_at_usec", 0)
                        <= journal.get("completed_at_usec", -1)
                        and SHA.fullmatch(str(journal.get("started_message_sha256", "")))
                        and SHA.fullmatch(str(journal.get("completed_message_sha256", "")))
                        and absence.get("unit_state", {}).get("LoadState") == "not-found"
                        and absence.get("matching_sessions") == 0
                        and SHA.fullmatch(exit_value.get("admission_inventory_sha256", ""))
                    )
            else:
                continue
            exit_digest = source_bundle._sha(archived_exit)
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
            and exit_digest == value.get("exit_sha256")
            and exit_proven
            and event.get("exit_sha256", value.get("exit_sha256")) == value.get("exit_sha256")
            and event.get("exit_source", value.get("exit_source", "worker-exit"))
            == value.get("exit_source", "worker-exit")
            and event.get("unit", value.get("unit")) == value.get("unit")
            and event.get("invocation", value.get("invocation")) == value.get("invocation")
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


def _prestart_state(unit: str, host: str, card: str) -> dict:
    """Require an absent service and no matching destination tmux session."""
    import subprocess

    state = unit_terminal(unit, "0" * 32, host=host)
    if state.get("LoadState") != "not-found":
        raise ValueError("review worker unit is present; prestart retirement refused")
    command = ["tmux", "ls", "-F", "#{session_name}"]
    if host != socket.gethostname().split(".")[0].lower():
        command = [
            "ssh",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=5",
            host,
            *command,
        ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode not in {0, 1}:
        raise ValueError("destination session inventory unavailable")
    sessions = result.stdout.splitlines()
    if any(name.endswith("-" + card) for name in sessions):
        raise ValueError("matching destination session exists; prestart retirement refused")
    return {
        "schema": PRESTART_SCHEMA,
        "unit": unit,
        "unit_state": state,
        "session_inventory_sha256": source_bundle._sha(result.stdout.encode()),
        "matching_sessions": 0,
    }


def _journal_terminal(
    unit: str, host: str, card: str, owner: str, claim: str, request_id: str, invocation: str
):
    """Bind one collected transient unit run to its sealed review request."""
    import subprocess

    command = ["journalctl", "--user", "--unit", unit, "--no-pager", "--output=json"]
    if host != socket.gethostname().split(".")[0].lower():
        command = [
            "ssh",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=5",
            host,
            *command,
        ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode not in {0, 1}:
        raise ValueError("worker journal inventory unavailable")
    try:
        records = [json.loads(line) for line in result.stdout.splitlines() if line]
    except ValueError as exc:
        raise ValueError("worker journal inventory malformed") from exc
    exact = [
        row
        for row in records
        if row.get("USER_UNIT") == unit
        and row.get("USER_INVOCATION_ID") == invocation
        and row.get("_HOSTNAME") == host
    ]
    started = [
        row
        for row in exact
        if row.get("CODE_FUNC") == "job_emit_done_message"
        and row.get("JOB_TYPE") == "start"
        and row.get("JOB_RESULT") == "done"
    ]
    completed = [
        row
        for row in exact
        if row.get("CODE_FUNC") == "unit_log_resources"
        and str(row.get("MESSAGE", "")).startswith(unit + ": Consumed ")
    ]
    if len(started) != 1 or len(completed) != 1:
        raise ValueError("one exact collected worker invocation required")
    try:
        started_at = int(started[0]["__REALTIME_TIMESTAMP"])
        completed_at = int(completed[0]["__REALTIME_TIMESTAMP"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("worker journal timestamps are missing") from exc
    if completed_at < started_at:
        raise ValueError("worker terminal record predates its start")
    return {
        "schema": "skfleet.collected-worker-terminal/v1",
        "unit": unit,
        "host": host,
        "card_id": card,
        "owner": owner,
        "claim_revision": claim,
        "request_id": request_id,
        "invocation": invocation,
        "started_at_usec": started_at,
        "started_message_sha256": source_bundle._sha(
            json.dumps(
                {
                    key: started[0].get(key)
                    for key in ("USER_UNIT", "USER_INVOCATION_ID", "JOB_TYPE", "JOB_RESULT")
                },
                sort_keys=True,
            ).encode()
        ),
        "completed_at_usec": completed_at,
        "completed_message_sha256": source_bundle._sha(completed[0]["MESSAGE"].encode()),
    }


def _admission_inventory(
    home: Path,
    host: str,
    card: str,
    owner: str,
    claim: str,
    request_id: str,
    *,
    terminal: dict | None = None,
    unit: str | None = None,
):
    """Hash the host's intent inventory and refuse the exact review generation."""
    from . import production_admission as admission

    root = Path(home) / "fleet/resource-admission" / host
    if not os.path.lexists(root):
        return source_bundle._sha(b"[]")
    if root.is_symlink() or not root.is_dir():
        raise ValueError("resource admission inventory is ambiguous")
    rows = []
    for directory in sorted(root.iterdir()):
        if directory.name == ".lock":
            if not admission._private_lock_stat(directory.lstat()):
                raise ValueError("resource admission lock is unsafe")
            continue
        if directory.is_symlink() or not directory.is_dir() or not SHA.fullmatch(directory.name):
            raise ValueError("resource admission inventory is ambiguous")
        intent_path = directory / "intent.json"
        if intent_path.is_symlink() or not intent_path.is_file():
            raise ValueError("resource admission intent inventory is incomplete")
        intent = admission.read_json(intent_path)
        if not isinstance(intent, dict) or admission._reservation_id(intent) != directory.name:
            raise ValueError("resource admission intent identity is invalid")
        binding = intent.get("binding", {})
        matching = binding.get("request_id") == request_id or (
            binding.get("card_id") == card
            and binding.get("owner") == owner
            and binding.get("claim_revision") == claim
        )
        if matching:
            start_path = directory / "start.json"
            try:
                start = admission.read_json(start_path)
            except (OSError, ValueError):
                start = None
            if not (
                terminal
                and intent.get("host") == host
                and intent.get("unit") == unit
                and binding.get("card_id") == card
                and binding.get("owner") == owner
                and binding.get("claim_revision") == claim
                and binding.get("request_id") == request_id
                and binding.get("work_kind") == "review"
                and start
                == {
                    "schema": "skfleet.resource-start/v1",
                    "reservation_id": directory.name,
                    "binding": binding,
                    "argv_sha256": intent.get("argv_sha256"),
                }
                and terminal.get("unit") == unit
                and terminal.get("host") == host
                and terminal.get("card_id") == card
                and terminal.get("owner") == owner
                and terminal.get("claim_revision") == claim
                and terminal.get("request_id") == request_id
            ):
                raise ValueError("matching resource admission intent exists")
        rows.append([directory.name, source_bundle._sha(_raw(intent_path))])
    return source_bundle._sha(json.dumps(rows, separators=(",", ":")).encode())


def retire_prestart(
    paths,
    home: Path,
    node: str,
    card: str,
    *,
    request_sha256: str,
    card_sha256: str,
    previous_owner: str,
    previous_claim_revision: str,
    actor: str,
    reason: str,
    apply: bool = False,
) -> dict:
    """Archive an exact stale offer only after its reviewer claim was released."""
    home = Path(home)
    policy = production_builder.policy()
    if not policy or policy["authority_host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production authority required")
    if (
        not dispatch.valid_name(node)
        or not CARD.fullmatch(card)
        or not dispatch.valid_name(previous_owner)
        or not re.fullmatch(r"[0-9a-f]{32}", previous_claim_revision)
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,95}", actor)
        or not 1 <= len(reason.strip()) <= 1024
        or not SHA.fullmatch(request_sha256)
        or not SHA.fullmatch(card_sha256)
    ):
        raise ValueError("exact prestart retirement identity and attribution required")
    request_path = dispatch.request_path(paths, node, card)
    status_path = dispatch.status_path(paths, node, card)
    with (
        dispatch._request_exclusion(paths.root / "dispatch/.production-offer"),
        dispatch._request_exclusion(request_path),
        card_mutation_lock(home, card),
    ):
        if os.path.lexists(status_path):
            raise ValueError("status already exists; prestart retirement refused")
        raw_request = _raw(request_path)
        if source_bundle._sha(raw_request) != request_sha256:
            raise ValueError("sealed request hash changed")
        request = json.loads(raw_request)
        request_id = request.get("request_id")
        if (
            request.get("schema") != "skfleet.builder-dispatch/v2"
            or request.get("work_kind") != "review"
            or request.get("card_id") != card
            or request.get("node") != node
            or request.get("reviewer") != previous_owner
            or not isinstance(request_id, str)
            or not SHA.fullmatch(request_id)
        ):
            raise ValueError("exact sealed review request required")
        events = CardStore(home)._read_events(card)
        releases = [
            row
            for row in events
            if row.get("action") == "release_claim"
            and row.get("writer") == actor
            and row.get("released_owner") == previous_owner
            and row.get("expected_claim_revision") == previous_claim_revision
        ]
        offers = [
            row
            for row in events
            if row.get("action") == "remote_review_offer"
            and row.get("request_id") == request_id
            and row.get("request_sha256") == production_builder.digest(request)
        ]
        launches = [
            row
            for row in events
            if row.get("action") == "review_assignment_launch"
            and row.get("recommendation_id") == request_id
        ]
        if len(releases) != 1 or len(offers) != 1 or launches:
            raise ValueError("exact release and unused offer proof required")
        store = CardStore(home)
        current = store.fold(card)
        if (
            current is None
            or current.archived
            or current.owner is not None
            or current.meta.get("claim_conflicts")
            or not governed_review_assignment_ready(current)
            or review_state_revision(current) != card_sha256
            or {"hold", "human-gate", "do-not-claim"}.intersection(current.labels)
        ):
            raise ValueError("unclaimed review card changed")
        core = current.model_dump(mode="json")
        criteria = production_builder.digest(
            {
                key: core.get(key)
                for key in (
                    "title",
                    "description",
                    "acceptance_criteria",
                    "labels",
                    "dependencies",
                    "links",
                )
            }
        )
        if criteria != request.get("criteria_sha256"):
            raise ValueError("review criteria changed after offer")
        from . import review_dispatch

        if review_dispatch._source(home, current) != request.get("source"):
            raise ValueError("source changed after offer")
        exit_path = (
            Path(home)
            / "evidence/production-review-exits"
            / (f"{card}-{previous_claim_revision}.json")
        )
        if exit_path.exists():
            raise ValueError("review exit exists; prestart retirement refused")
        host = production_builder.node_binding(paths, node, policy)["host"]
        if request.get("production", {}).get("host") != host:
            raise ValueError("sealed review destination changed")
        unit = "skfleet-worker-" + request["production"]["family"] + "-" + card + ".service"
        admission_inventory_sha256 = _admission_inventory(
            home, host, card, previous_owner, previous_claim_revision, request_id
        )
        proof = _prestart_state(unit, host, card)
        proof["admission_inventory_sha256"] = admission_inventory_sha256
        proof["dispatch_status_absent"] = not os.path.lexists(status_path)
        if not proof["dispatch_status_absent"]:
            raise ValueError("status appeared during prestart check")
        if os.path.lexists(status_path) or request_path.read_bytes() != raw_request:
            raise ValueError("review request changed during prestart check")
        if (
            _admission_inventory(
                home, host, card, previous_owner, previous_claim_revision, request_id
            )
            != admission_inventory_sha256
        ):
            raise ValueError("resource admission inventory changed during prestart check")
        binding = {
            "schema": PRESTART_SCHEMA,
            "card_id": card,
            "node": node,
            "request_id": request_id,
            "request_sha256": request_sha256,
            "card_sha256": card_sha256,
            "previous_owner": previous_owner,
            "previous_claim_revision": previous_claim_revision,
            "prestart_sha256": source_bundle._sha(json.dumps(proof, sort_keys=True).encode()),
            "actor": actor,
            "reason": reason.strip(),
        }
        if not apply:
            return {"state": "qualified-check-only", "binding": binding}
        receipt = _receipt_path(home, card, request_id)
        source_bundle._once(receipt.parent / "request.json", raw_request)
        proof_raw = json.dumps(proof, sort_keys=True).encode()
        source_bundle._once(receipt.parent / "prestart.json", proof_raw)
        source_bundle._once(receipt, json.dumps(binding, sort_keys=True).encode())
        store.append_event(
            card,
            "remote_review_prestart_retire",
            actor,
            schema=PRESTART_SCHEMA,
            request_id=request_id,
            request_sha256=request_sha256,
            offer_request_sha256=production_builder.digest(request),
            prestart_sha256=source_bundle._sha(proof_raw),
            card_sha256=card_sha256,
            previous_owner=previous_owner,
            previous_claim_revision=previous_claim_revision,
            actor=actor,
            reason=reason.strip(),
            receipt_sha256=source_bundle._sha(_raw(receipt)),
        )
        _remove_pointer(request_path)
        return {"state": "retired-prestart", "receipt": str(receipt)}


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
        if len(exits) > 1:
            raise ValueError("one exact failed worker exit required")
        unit = unit_terminal(execution["unit"], execution["invocation"], host=host)
        systemd_failure = (
            not exits
            and unit.get("InvocationID") == execution["invocation"]
            and unit.get("LoadState") == "loaded"
            and unit.get("ActiveState") == "failed"
            and unit.get("SubState") == "failed"
            and unit.get("MainPID") == "0"
            and unit.get("ControlPID") == "0"
            and unit.get("Result") not in {None, "", "success"}
            and unit.get("ExecMainStatus") not in {None, "", "0"}
        )
        journal_terminal = None
        absence = None
        admission_inventory_sha256 = None
        if not exits and not systemd_failure and unit.get("LoadState") == "not-found":
            absence = _prestart_state(execution["unit"], host, card)
            journal_terminal = _journal_terminal(
                execution["unit"],
                host,
                card,
                status["owner"],
                status["claim_revision"],
                request_id,
                execution["invocation"],
            )
            admission_inventory_sha256 = _admission_inventory(
                home,
                host,
                card,
                status["owner"],
                status["claim_revision"],
                request_id,
                terminal=journal_terminal,
                unit=execution["unit"],
            )
        if not exits and not systemd_failure:
            if journal_terminal is None:
                raise ValueError("one exact failed worker exit required")
        if (
            review_state_revision(store.fold(card)) != card_sha256
            or _raw(request_path) != raw_request
            or _raw(status_path) != raw_status
        ):
            raise ValueError("review generation changed during terminal check")
        exit_source = "worker-exit" if exits else "systemd-terminal"
        if exits:
            exit_evidence = exits[0][0]
        elif journal_terminal is not None:
            exit_evidence = json.dumps(
                {
                    "unit": execution["unit"],
                    "invocation": execution["invocation"],
                    "journal": journal_terminal,
                    "absence": absence,
                    "admission_inventory_sha256": admission_inventory_sha256,
                },
                sort_keys=True,
            ).encode()
        else:
            exit_evidence = json.dumps(unit, sort_keys=True).encode()
        binding = dict(
            schema=SCHEMA,
            card_id=card,
            node=node,
            request_id=request_id,
            request_sha256=request_sha256,
            status_sha256=status_sha256,
            offer_request_sha256=production_builder.digest(request),
            card_sha256=card_sha256,
            exit_sha256=source_bundle._sha(exit_evidence),
            exit_source=exit_source,
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
        exit_name = "worker-exit.json" if exits else "systemd-terminal.json"
        source_bundle._once(receipt.parent / exit_name, exit_evidence)
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
            exit_sha256=source_bundle._sha(exit_evidence),
            exit_source=exit_source,
            unit=execution["unit"],
            invocation=execution["invocation"],
            actor=actor,
            reason=reason.strip(),
            receipt_sha256=source_bundle._sha(_raw(receipt)),
        )
        # Remove the node status first: a crash leaves the request holding the card.
        _remove_pointer(status_path)
        _remove_pointer(request_path)
        return {"state": "retired", "receipt": str(receipt)}
