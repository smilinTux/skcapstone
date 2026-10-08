"""Short host-local admission transactions, independent of generation ownership.

An unobserved launch reserves its full RAM allowance. Once its exact marked
service is observed live, existing cgroup accounting takes over. Durable intents
are never replayed or aged out. This is admission, not claim or lifecycle policy.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

from skcoord.card_store import CardStore, card_mutation_lock

from ..seraph_review_cardstore import card_revision
from .production_resources import (
    active_resource_units,
    local_worker_admission,
    successful_terminal_absent,
    successful_terminal_state,
)
from .production_test_plan import private_dir, read_json, write_once

MARKER = "SKFLEET_ADMISSION_ID"
_JOURNAL_TERMINAL_IDS = {
    "9d1aaa27d60140bd96365438aad20286",  # Stopped
    "d9b373ed55a64feb8242e02dbe79a49c",  # Failed
    "ae8f7b866b0347b9af31fe1c80b127c0",  # Unit runtime completed; resources consumed
}


class AdmissionError(ValueError):
    """Admission is unavailable; existing claims and launch custody stay owned."""


class AdmissionDeferredError(AdmissionError):
    """Measured RAM is insufficient; no intent or spawn exists for this generation."""


def unit_state(unit: str, *, terminal: bool = False) -> dict:
    """Read a unit's marker without logging environment contents."""
    result = subprocess.run(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=Id,LoadState,ActiveState,SubState,InvocationID,Environment,MemoryMax,MemoryCurrent"
            + (
                ",SubState,MainPID,ControlPID,ExecMainPID,ExecMainCode,ExecMainStatus,ControlGroup,TasksCurrent,Result"
                if terminal
                else ""
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    environment = shlex.split(values.pop("Environment", ""))
    markers = [item.split("=", 1)[1] for item in environment if item.startswith(MARKER + "=")]
    values[MARKER] = markers[0] if len(markers) == 1 else ""
    return values


@contextmanager
def _transaction(home: Path, host: str):
    """Hold only the capacity observation and durable intent publication lock."""
    if host != socket.gethostname().split(".")[0].lower():
        raise AdmissionError("admission belongs to another host")
    root = Path(home) / "fleet/resource-admission"
    private_dir(root, create=True)
    root = root / host
    private_dir(root, create=True)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        if not _private_lock_stat(info):
            raise AdmissionError("admission lock is not private")
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield root


def _private_lock_stat(info: os.stat_result) -> bool:
    """Match the ownership and file shape required for an admission lock."""
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and info.st_nlink == 1
    )


def _digest(value: dict) -> str:
    """Name a non-reusable reservation from its exact source and quota binding."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _intent(policy: dict, host: str, unit: str, binding: dict, argv: list[str]) -> dict:
    """Bind the unchanged command bytes, source identity and qualified quotas."""
    return {
        "schema": "skfleet.resource-admission/v1",
        "host": host,
        "unit": unit,
        "binding": binding,
        "resources": dict(policy["node_quotas"][host]),
        "argv_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
    }


def _reservation_id(intent: dict) -> str:
    """Prevent changed command or quota bytes from replaying one source generation."""
    return _digest({key: intent[key] for key in ("host", "unit", "binding")})


def reserved_command(
    home: Path, policy: dict, host: str, unit: str, binding: dict, argv: list[str]
) -> list[str]:
    """Verify an existing immutable reservation when checking launch receipts."""
    intent = _intent(policy, host, unit, binding, argv)
    identity = _reservation_id(intent)
    path = Path(home) / "fleet/resource-admission" / host / identity / "intent.json"
    if read_json(path) != intent:
        raise AdmissionError("reservation command binding changed")
    return [argv[0], "--setenv=" + MARKER + "=" + identity, *argv[1:]]


def _occupancy(root: Path, home: Path, *, strict_terminal=False) -> list[dict]:
    """Account live units plus unobserved intents, without double charging RAM."""
    units = {row["unit"]: dict(row) for row in active_resource_units(home)}
    for directory in sorted(root.iterdir()):
        if directory.name == ".lock":
            continue
        private_dir(directory)
        intent = read_json(directory / "intent.json")
        if _reservation_id(intent) != directory.name:
            raise AdmissionError("reservation identity is inconsistent")
        unit, maximum = intent["unit"], intent["resources"]["memory_max_bytes"]
        if strict_terminal and unit not in units:
            # Historical manager proof already binds the exact invocation. Live
            # inventory above still charges any later reuse of this unit name.
            journal_path = directory / "journal-terminal.json"
            if journal_path.exists() and (directory / "observed.json").exists():
                observed = read_json(directory / "observed.json")
                if not _valid_observed(observed, intent):
                    raise AdmissionError("reservation acknowledgment is inconsistent")
                if not _valid_journal_terminal(read_json(journal_path), intent, observed):
                    raise AdmissionError("journal terminal proof differs")
                continue
        from .production_legacy_terminal import reconcile_legacy_assignment

        legacy_terminal = reconcile_legacy_assignment(directory, home, intent, live=unit in units)
        if legacy_terminal is True:
            continue
        if legacy_terminal is False:
            units.setdefault(unit, {"unit": unit, "reserved_memory_max": maximum})
            continue
        if (directory / "success-terminal.json").exists():
            proof = read_json(directory / "success-terminal.json")
            if not _valid_success_receipt(proof, intent):
                raise AdmissionError("successful launch terminal receipt is inconsistent")
            continue
        prestart = directory / "released-prestart.json"
        if prestart.exists():
            proof = read_json(prestart)
            if not _released_prestart(home, intent, proof):
                raise AdmissionError("released prestart proof differs")
            continue
        if (
            unit not in units
            and not (directory / "observed.json").exists()
            and _recover_unobserved_builder(directory, intent)
        ):
            continue
        if strict_terminal and unit not in units and not (directory / "start.json").exists():
            proof = _released_prestart_proof(home, intent)
            if proof is not None:
                write_once(prestart, proof)
                continue
        if strict_terminal and unit not in units:
            terminal_path = directory / "terminal.json"
            if terminal_path.exists():
                proof = read_json(terminal_path)
                if proof.get("intent_sha256") != _digest(intent) or not _terminal_state(
                    proof.get("state", {}), intent
                ):
                    raise AdmissionError("terminal admission proof differs")
                continue
            state = unit_state(unit, terminal=True)
            if _terminal_state(state, intent):
                if unit_state(unit, terminal=True) != state:
                    raise AdmissionError("terminal admission observation changed")
                write_once(terminal_path, {"intent_sha256": _digest(intent), "state": state})
                continue
        if (directory / "failed-terminal.json").exists():
            proof = read_json(directory / "failed-terminal.json")
            if not _valid_failed_receipt(proof, intent):
                raise AdmissionError("failed launch terminal receipt is inconsistent")
            # Any current service remains charged by the live inventory above.
            continue
        if (directory / "observed.json").exists():
            observed = read_json(directory / "observed.json")
            if not _valid_observed(observed, intent):
                raise AdmissionError("reservation acknowledgment is inconsistent")
            if strict_terminal and unit not in units:
                journal_path = directory / "journal-terminal.json"
                if journal_path.exists():
                    if not _valid_journal_terminal(read_json(journal_path), intent, observed):
                        raise AdmissionError("journal terminal proof differs")
                    continue
                state = unit_state(unit, terminal=True)
                if not (
                    state.get("Id") == unit
                    and state.get("LoadState") == "loaded"
                    and state.get("InvocationID") == observed["invocation"]
                    and state.get(MARKER) == directory.name
                    and state.get("ActiveState") in {"inactive", "failed"}
                    and state.get("SubState") in {"dead", "failed"}
                    and state.get("MainPID") == "0"
                    and state.get("ControlPID") == "0"
                    and state.get("TasksCurrent") in {"0", "[not set]"}
                    and state.get("ControlGroup") == ""
                ):
                    proof = _journal_terminal_proof(intent, observed, state)
                    if proof is not None:
                        write_once(journal_path, proof)
                        continue
                    units[unit] = {"unit": unit, "reserved_memory_max": maximum}
            continue
        if unit in units:
            state = unit_state(unit)
            if state.get("Id") != unit:
                raise AdmissionError("pending service response is inconsistent")
            if state.get("ActiveState") in {"inactive", "failed"}:
                units[unit]["reserved_memory_max"] = maximum
                continue
            if state.get("SubState") == "exited":
                # Only the explicit claim-fenced finalizer discharges this intent.
                units[unit]["reserved_memory_max"] = maximum
                continue
            if (
                state.get("Id") != unit
                or state.get("LoadState") != "loaded"
                or state.get("ActiveState") not in {"active", "activating"}
                or state.get(MARKER) != directory.name
                or int(state.get("MemoryMax", "0")) != maximum
                or not re.fullmatch(r"[0-9]+", state.get("MemoryCurrent", ""))
                or not re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
            ):
                raise AdmissionError("pending service identity or quota is uncertain")
            # The same live list is passed to admission below. After a crash here,
            # a fresh list still accounts this service; an absent unit has exited.
            write_once(
                directory / "observed.json",
                {
                    "reservation_id": directory.name,
                    "unit": unit,
                    "invocation": state["InvocationID"],
                    "memory_max_bytes": maximum,
                },
            )
        else:
            units[unit] = {"unit": unit, "reserved_memory_max": maximum}
    return list(units.values())


def _released_prestart_proof(home: Path, intent: dict) -> dict | None:
    """Retire only an unstarted intent whose exact native claim was released."""
    binding = intent["binding"]
    store = CardStore(home)
    card = store.fold(binding["card_id"])
    releases = [
        event
        for event in store._read_events(binding["card_id"])
        if event.get("action") == "release_claim"
        and event.get("released_owner") == binding["owner"]
        and event.get("expected_claim_revision") == binding["claim_revision"]
    ]
    if (
        card is None
        or not releases
        or (card.owner, card.meta.get("_claim_revision"))
        == (binding["owner"], binding["claim_revision"])
    ):
        return None
    state = unit_state(intent["unit"], terminal=True)
    if not _unstarted_unit_state(state, intent):
        return None
    return {
        "schema": "skfleet.released-prestart/v1",
        "reservation_id": _reservation_id(intent),
        "intent_sha256": _digest(intent),
        "release_sha256": _digest(releases[-1]),
        "state": state,
    }


def _unstarted_unit_state(state: dict, intent: dict) -> bool:
    return (
        isinstance(state, dict)
        and state.get("Id") == intent["unit"]
        and state.get("LoadState") == "not-found"
        and state.get("ActiveState") == "inactive"
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
        and state.get("ControlGroup") == ""
        and state.get("InvocationID") == ""
    )


def _released_prestart(home: Path, intent: dict, proof: dict) -> bool:
    if (
        proof.get("schema") != "skfleet.released-prestart/v1"
        or proof.get("reservation_id") != _reservation_id(intent)
        or proof.get("intent_sha256") != _digest(intent)
        or not _unstarted_unit_state(proof.get("state"), intent)
    ):
        return False
    binding = intent["binding"]
    return any(
        _digest(event) == proof.get("release_sha256")
        and event.get("action") == "release_claim"
        and event.get("released_owner") == binding["owner"]
        and event.get("expected_claim_revision") == binding["claim_revision"]
        for event in CardStore(home)._read_events(binding["card_id"])
    )


def _valid_observed(observed: dict, intent: dict) -> bool:
    return (
        observed.get("reservation_id") == _reservation_id(intent)
        and observed.get("unit") == intent["unit"]
        and observed.get("memory_max_bytes") == intent["resources"]["memory_max_bytes"]
        and bool(re.fullmatch(r"[0-9a-f]{32}", str(observed.get("invocation", ""))))
    )


def _valid_journal_terminal(proof: dict, intent: dict, observed: dict) -> bool:
    """A collected service needs a terminal event for its exact invocation."""
    return (
        proof.get("schema") == "skfleet.journal-admission-terminal/v1"
        and proof.get("intent_sha256") == _digest(intent)
        and proof.get("reservation_id") == _reservation_id(intent)
        and proof.get("unit") == intent["unit"]
        and proof.get("invocation") == observed["invocation"]
        and proof.get("message_id") in _JOURNAL_TERMINAL_IDS
        and bool(re.fullmatch(r"[0-9]+", str(proof.get("realtime_us", ""))))
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("entry_sha256", ""))))
    )


def _recover_unobserved_builder(directory: Path, intent: dict) -> bool:
    """Recover a unique consumed builder start that systemd collected before observation."""
    binding = intent["binding"]
    card = binding.get("card_id")
    request, attempt = binding.get("request_id"), binding.get("attempt")
    if "request_id" not in binding and "attempt" not in binding:
        plan_sha256 = binding.get("plan_sha256")
        if re.fullmatch(r"[0-9a-f]{64}", str(plan_sha256)):
            request, attempt = plan_sha256, 1
    if (
        not re.fullmatch(r"[0-9a-f]{8}", str(card))
        or not re.fullmatch(r"[0-9a-f]{64}", str(request))
        or type(attempt) is not int
        or attempt < 1
        or intent["unit"] != f"skfleet-builder-{card}-{request}-{attempt}.service"
        or not (directory / "start.json").exists()
        or read_json(directory / "start.json")
        != {
            "schema": "skfleet.resource-start/v1",
            "reservation_id": _reservation_id(intent),
            "binding": binding,
            "argv_sha256": intent["argv_sha256"],
        }
    ):
        return False
    state = unit_state(intent["unit"], terminal=True)
    if state.get("LoadState") != "not-found":
        return False
    started = "39f53479d3a045ac8e11786248231fbf"
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "--quiet",
                "--no-pager",
                "--output=json",
                "USER_UNIT=" + intent["unit"],
                *("MESSAGE_ID=" + key for key in sorted(_JOURNAL_TERMINAL_IDS | {started})),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode or len(result.stdout) > 65536:
            return False
        lines = result.stdout.splitlines()
        events = [json.loads(line) for line in lines]
        if any(not isinstance(event, dict) for event in events):
            return False
        events = [event for event in events if event.get("USER_UNIT") == intent["unit"]]
        starts = [event for event in events if event.get("MESSAGE_ID") == started]
        invocations = {event.get("USER_INVOCATION_ID") for event in events}
        if len(starts) != 1 or len(invocations) != 1:
            return False
        invocation = starts[0].get("USER_INVOCATION_ID")
        if not re.fullmatch(r"[0-9a-f]{32}", str(invocation)):
            return False
        start_time = int(starts[0]["__REALTIME_TIMESTAMP"])
        if start_time <= 0 or not any(
            event.get("MESSAGE_ID") in _JOURNAL_TERMINAL_IDS
            and int(event["__REALTIME_TIMESTAMP"]) > start_time
            for event in events
        ):
            return False
        terminal_lines = [
            line for line in lines if int(json.loads(line)["__REALTIME_TIMESTAMP"]) > start_time
        ]
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, KeyError):
        return False
    observed = dict(
        reservation_id=_reservation_id(intent),
        unit=intent["unit"],
        invocation=invocation,
        memory_max_bytes=intent["resources"]["memory_max_bytes"],
    )
    proof = _journal_terminal_proof(intent, observed, state, entries=terminal_lines)
    if proof is None:
        return False
    proof["start_realtime_us"] = starts[0]["__REALTIME_TIMESTAMP"]
    proof["start_entry_sha256"] = next(
        hashlib.sha256(line.encode()).hexdigest()
        for line in lines
        if json.loads(line) == starts[0]
    )
    write_once(directory / "journal-terminal.json", proof)
    write_once(directory / "observed.json", observed)
    return True


def _journal_terminal_proof(
    intent: dict, observed: dict, state: dict, *, entries: list[str] | None = None
) -> dict | None:
    """Recover systemd-collected terminal custody without treating absence as proof."""
    if not (
        state.get("Id") == intent["unit"]
        and state.get("LoadState") in {"loaded", "not-found"}
        and state.get("ActiveState") in {"inactive", "failed"}
        and state.get("SubState") in {"dead", "failed"}
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
        and state.get("TasksCurrent") in {"0", "[not set]"}
        and state.get("ControlGroup") == ""
    ):
        return None
    if entries is None:
        try:
            result = subprocess.run(
                [
                    "journalctl",
                    "--user",
                    "--quiet",
                    "--no-pager",
                    "--output=json",
                    "USER_UNIT=" + intent["unit"],
                    "USER_INVOCATION_ID=" + observed["invocation"],
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        entries = result.stdout.splitlines()
    for line in entries:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if (
            event.get("USER_UNIT") == intent["unit"]
            and event.get("USER_INVOCATION_ID") == observed["invocation"]
            and event.get("MESSAGE_ID") in _JOURNAL_TERMINAL_IDS
        ):
            proof = {
                "schema": "skfleet.journal-admission-terminal/v1",
                "intent_sha256": _digest(intent),
                "reservation_id": _reservation_id(intent),
                "unit": intent["unit"],
                "invocation": observed["invocation"],
                "message_id": event["MESSAGE_ID"],
                "realtime_us": event.get("__REALTIME_TIMESTAMP"),
                "entry_sha256": hashlib.sha256(line.encode()).hexdigest(),
            }
            if (
                _valid_journal_terminal(proof, intent, observed)
                and unit_state(intent["unit"], terminal=True) == state
            ):
                return proof
    return None


def _terminal_state(state, intent):
    """Require a retained marked invocation with no remaining process or cgroup."""
    return (
        state.get("Id") == intent["unit"]
        and state.get("LoadState") == "loaded"
        and state.get(MARKER) == _reservation_id(intent)
        and state.get("MemoryMax") == str(intent["resources"]["memory_max_bytes"])
        and bool(re.fullmatch(r"[0-9a-f]{32}", str(state.get("InvocationID", ""))))
        and state.get("ActiveState") in {"inactive", "failed"}
        and state.get("SubState") in {"dead", "failed"}
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
        and state.get("TasksCurrent") in {"0", "[not set]"}
        and state.get("ControlGroup") == ""
        and state.get("ExecMainCode") in {"1", "2", "3"}
        and bool(re.fullmatch(r"[0-9]+", str(state.get("ExecMainStatus", ""))))
    )


def _failed_state(state: dict, intent: dict, invocation: str) -> bool:
    """Validate retained failed invocation metadata, never absence or elapsed time."""
    return (
        state.get("Id") == intent["unit"]
        and state.get("LoadState") == "loaded"
        and (state.get("ActiveState"), state.get("SubState")) == ("failed", "failed")
        and state.get("InvocationID") == invocation
        and bool(re.fullmatch(r"[0-9a-f]{32}", invocation))
        and state.get(MARKER) == _reservation_id(intent)
        and state.get("MemoryMax") == str(intent["resources"]["memory_max_bytes"])
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
        and bool(re.fullmatch(r"[1-9][0-9]*", state.get("ExecMainPID", "")))
        and state.get("ExecMainCode") in {"1", "2", "3"}
        and bool(re.fullmatch(r"[1-9][0-9]*", state.get("ExecMainStatus", "")))
        and state.get("Result")
        in {"exit-code", "signal", "core-dump", "timeout", "oom-kill", "resources"}
        and (
            (state.get("ControlGroup") == "" and state.get("TasksCurrent") == "[not set]")
            or (
                isinstance(state.get("ControlGroup"), str)
                and state["ControlGroup"].startswith("/")
                and ".." not in Path(state["ControlGroup"]).parts
                and Path(state["ControlGroup"]).name == intent["unit"]
                and state.get("TasksCurrent") == "0"
            )
        )
    )


def _valid_failed_receipt(proof: dict, intent: dict, *, successful: bool = False) -> bool:
    """Bind durable terminal evidence to every original source and command byte."""
    kind = "success" if successful else "failed"
    valid_state = _success_state if successful else _failed_state
    return (
        proof.get("schema") == "skfleet." + kind + "-admission-terminal/v1"
        and proof.get("intent_sha256") == _digest(intent)
        and proof.get("reservation_id") == _reservation_id(intent)
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("card_revision", ""))))
        and proof.get("process_absent") is True
        and proof.get("cgroup_empty") is True
        and isinstance(proof.get("state"), dict)
        and valid_state(proof["state"], intent, proof["state"].get("InvocationID", ""))
    )


def finalize_failed_launch(
    home: Path,
    policy: dict,
    host: str,
    unit: str,
    binding: dict,
    argv: list[str],
    *,
    invocation: str,
    expected_card_revision: str,
) -> dict:
    """Finalize exact retained failure, preserving its original public contract."""
    return _finalize_launch(
        home,
        policy,
        host,
        unit,
        binding,
        argv,
        invocation=invocation,
        expected_card_revision=expected_card_revision,
    )


def finalize_successful_launch(
    home: Path,
    policy: dict,
    host: str,
    unit: str,
    binding: dict,
    argv: list[str],
    *,
    invocation: str,
    expected_card_revision: str,
) -> dict:
    """Discharge exact terminal resource custody, never accept product results."""
    return _finalize_launch(
        home,
        policy,
        host,
        unit,
        binding,
        argv,
        invocation=invocation,
        expected_card_revision=expected_card_revision,
        successful=True,
    )


def _success_state(state: dict, intent: dict, invocation: str) -> bool:
    """Bind retained successful metadata to the immutable admitted invocation."""
    return (
        state.get("Id") == intent["unit"]
        and state.get("InvocationID") == invocation
        and state.get(MARKER) == _reservation_id(intent)
        and state.get("MemoryMax") == str(intent["resources"]["memory_max_bytes"])
        and successful_terminal_state(state)
    )


def _valid_success_receipt(proof: dict, intent: dict) -> bool:
    """Validate historical proof without reinterpreting a later process lifetime."""
    return _valid_failed_receipt(proof, intent, successful=True)


def _finalize_launch(
    home: Path,
    policy: dict,
    host: str,
    unit: str,
    binding: dict,
    argv: list[str],
    *,
    invocation: str,
    expected_card_revision: str,
    successful: bool = False,
) -> dict:
    """Finalize only a retained failed invocation under unchanged native custody.

    The caller supplies its exact original command and source binding. No launch
    is replayed, unit stopped, claim released or absent intent aged out. A lost
    reply may reread the same immutable receipt while that exact claim remains.
    """
    intent = _intent(policy, host, unit, binding, argv)
    identity = _reservation_id(intent)
    if not re.fullmatch(r"[0-9a-f]{8}", str(binding.get("card_id", ""))):
        raise AdmissionError("failed launch card identity invalid")
    with card_mutation_lock(home, binding["card_id"]), _transaction(home, host) as root:
        directory = root / identity
        if read_json(directory / "intent.json") != intent:
            raise AdmissionError("failed launch source or command binding changed")
        card = CardStore(home).fold(binding["card_id"])
        if (
            card is None
            or card.archived
            or card.meta.get("claim_conflicts")
            or card.status.value != "doing"
            or card.owner != binding["owner"]
            or card.meta.get("_claim_revision") != binding["claim_revision"]
            or card_revision(card) != expected_card_revision
        ):
            raise AdmissionError("failed launch native custody changed")
        validate = _valid_success_receipt if successful else _valid_failed_receipt
        valid_state = _success_state if successful else _failed_state
        kind = "success" if successful else "failed"
        path = directory / (kind + "-terminal.json")
        if successful and read_json(directory / "start.json") != {
            "schema": "skfleet.resource-start/v1",
            "reservation_id": identity,
            "binding": binding,
            "argv_sha256": intent["argv_sha256"],
        }:
            raise AdmissionError("successful launch lacks exact consumed start")
        if path.exists():
            proof = read_json(path)
            if (
                not validate(proof, intent)
                or proof["state"]["InvocationID"] != invocation
                or proof["card_revision"] != expected_card_revision
            ):
                raise AdmissionError("failed launch receipt changed")
            return proof
        if (directory / "observed.json").exists():
            if not successful:
                raise AdmissionError("launch already observed; normal lifecycle owns termination")
            if read_json(directory / "observed.json") != {
                "reservation_id": identity,
                "unit": unit,
                "invocation": invocation,
                "memory_max_bytes": intent["resources"]["memory_max_bytes"],
            }:
                raise AdmissionError("successful launch observed invocation changed")
        before = unit_state(unit, terminal=True)
        if not valid_state(before, intent, invocation):
            raise AdmissionError("exact retained failed invocation unavailable")
        if successful and not successful_terminal_absent(before):
            raise AdmissionError("successful launch process or cgroup is not absent")
        if Path("/proc", before["ExecMainPID"]).exists():
            raise AdmissionError("failed launch process still exists")
        group = before["ControlGroup"]
        if group:
            cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
            events = cgroup / "cgroup.events"
            if cgroup.exists() and (
                not events.is_file() or "populated 0" not in events.read_text().splitlines()
            ):
                raise AdmissionError("failed launch cgroup is not empty")
        if unit_state(unit, terminal=True) != before:
            raise AdmissionError("failed launch invocation changed during observation")
        proof = dict(
            schema="skfleet." + kind + "-admission-terminal/v1",
            reservation_id=identity,
            intent_sha256=_digest(intent),
            card_revision=expected_card_revision,
            state=before,
            process_absent=True,
            cgroup_empty=True,
        )
        if not validate(proof, intent):
            raise AdmissionError("failed launch terminal proof is invalid")
        write_once(path, proof)
        return proof


def reserve_launch(
    home: Path, policy: dict, host: str, unit: str, binding: dict, argv: list[str]
) -> list[str]:
    """Reserve before spawning and return an argv bearing the exact intent marker.

    Validate the current claim atomically with intent publication. Callers retain
    custody on every exception after reservation and use start_reserved for spawn.
    No caller may replay an existing intent.
    """
    try:
        if (
            not re.fullmatch(r"skfleet-(?:worker|builder)-[a-zA-Z0-9_.-]+\.service", unit)
            or not isinstance(binding, dict)
            or any(
                not isinstance(binding.get(key), str) or not binding[key]
                for key in ("card_id", "owner", "claim_revision")
            )
        ):
            raise AdmissionError("launch identity is invalid")
        from .production_policy import require_destination

        limits = require_destination(policy, host)
        keys = {"cpu_quota_percent", "memory_max_bytes", "tasks_max", "runtime_max_seconds"}
        if set(limits) != keys or any(type(n) is not int or n <= 0 for n in limits.values()):
            raise AdmissionError("launch quota is invalid")
        expected = [
            f"--property=CPUQuota={limits['cpu_quota_percent']}%",
            f"--property=MemoryMax={limits['memory_max_bytes']}",
            f"--property=TasksMax={limits['tasks_max']}",
            f"--property=RuntimeMaxSec={limits['runtime_max_seconds']}",
        ]
        if "--" not in argv:
            raise AdmissionError("launch command lacks explicit option boundary")
        options = argv[: argv.index("--")]
        # Native callers use this one explicit option form. Refuse duplicates or
        # alternate property forms that could override the qualified allowance.
        if (
            not options
            or Path(options[0]).name != "systemd-run"
            or "--user" not in options
            or any(options.count(option) != 1 for option in expected)
            or any(
                arg.startswith(("-p", "-E", "-u")) or arg in {"--property", "--setenv"}
                for arg in options
            )
            or any(MARKER in arg or arg.startswith("--property=Restart=") for arg in options)
        ):
            raise AdmissionError("launch quota command is invalid")
        for name in ("CPUQuota", "MemoryMax", "TasksMax", "RuntimeMaxSec"):
            if sum(arg.startswith("--property=" + name + "=") for arg in options) != 1:
                raise AdmissionError("launch quota command is ambiguous")
        if (
            options.count("--unit=" + unit)
            + sum(options[index : index + 2] == ["--unit", unit] for index in range(len(options)))
            != 1
        ):
            raise AdmissionError("launch unit command is invalid")
        if sum(arg == "--unit" or arg.startswith("--unit=") for arg in options) != 1 or any(
            arg == "--scope"
            or arg == "--system"
            or arg.startswith(
                (
                    "--host",
                    "--machine",
                    "-H",
                    "-M",
                    "--uid",
                    "--gid",
                    "--property=User=",
                    "--property=Group=",
                    "--property=Restart",
                )
            )
            for arg in options
        ):
            raise AdmissionError("launch must use the local user service manager")
        intent = _intent(policy, host, unit, binding, argv)
        identity = _reservation_id(intent)
        with card_mutation_lock(home, binding["card_id"]), _transaction(home, host) as root:
            _require_claim(home, binding)
            directory = root / identity
            if directory.exists():
                raise AdmissionError("launch already reserved; exact custody required")
            limits_policy = policy.get("node_admission", {}).get(host, {})
            rows = _occupancy(root, home, strict_terminal=bool(limits_policy))
            protected = limits_policy.get("protected_service_bindings", {})
            charged = 0
            for row in rows:
                if row["unit"] in protected:
                    result = subprocess.run(
                        ["systemctl", "--user", "cat", row["unit"]],
                        capture_output=True,
                        check=True,
                        timeout=5,
                    )
                    if hashlib.sha256(result.stdout).hexdigest() != protected[row["unit"]]:
                        raise AdmissionError("protected service definition changed")
                else:
                    charged += 1
            cap = limits_policy.get("max_concurrent_workers")
            if cap is not None and charged >= cap:
                raise AdmissionDeferredError(
                    f"worker occupancy cap reached: charged={charged} cap={cap}"
                )
            if any(row["unit"] == unit for row in rows):
                raise AdmissionError("unit already occupied or pending")
            ready, reason = local_worker_admission(policy, host, rows)
            if not ready:
                if reason.startswith("memory_available="):
                    raise AdmissionDeferredError(reason)
                raise AdmissionError(reason)
            private_dir(directory, create=True)
            write_once(directory / "intent.json", intent)
            write_once(
                directory / "fenced-start-required.json",
                {
                    "reservation_id": identity,
                    "claim_fenced": True,
                },
            )
        return [argv[0], "--setenv=" + MARKER + "=" + identity, *argv[1:]]
    except AdmissionError:
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise AdmissionError("node-resource-evidence-unavailable") from exc


def _require_claim(home: Path, binding: dict) -> None:
    """Call only under the native claim mutation lock, including during spawn."""
    card = CardStore(home).fold(binding["card_id"])
    if (
        card is None
        or card.archived
        or card.meta.get("claim_conflicts")
        or card.status.value != "doing"
        or card.owner != binding["owner"]
        or card.meta.get("_claim_revision") != binding["claim_revision"]
    ):
        raise AdmissionError("launch native claim changed")


def start_reserved(home: Path, host: str, argv: list[str], spawn):
    """Consume one immutable reservation and fence claim changes across spawn.

    The callback only starts the process; it must not wait for worker completion.
    Failed or uncertain spawn retains the intent and consumed receipt for native
    reconciliation. No claim is released here.
    """
    try:
        prefix = "--setenv=" + MARKER + "="
        identity = argv[1].removeprefix(prefix)
        if not argv[1].startswith(prefix) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise AdmissionError("launch reservation marker invalid")
        directory = Path(home) / "fleet/resource-admission" / host / identity
        private_dir(directory.parent.parent)
        private_dir(directory.parent)
        private_dir(directory)
        intent = read_json(directory / "intent.json")
        original = [argv[0], *argv[2:]]
        if (
            intent["host"] != host
            or host != socket.gethostname().split(".")[0].lower()
            or _reservation_id(intent) != identity
            or intent["argv_sha256"] != hashlib.sha256(json.dumps(original).encode()).hexdigest()
        ):
            raise AdmissionError("launch reservation command changed")
        binding = intent["binding"]
        with card_mutation_lock(home, binding["card_id"]):
            _require_claim(home, binding)
            if read_json(directory / "intent.json") != intent:
                raise AdmissionError("launch reservation changed")
            if read_json(directory / "fenced-start-required.json") != {
                "reservation_id": identity,
                "claim_fenced": True,
            }:
                raise AdmissionError("launch reservation lacks fenced-start proof")
            write_once(
                directory / "start.json",
                {
                    "schema": "skfleet.resource-start/v1",
                    "reservation_id": identity,
                    "binding": binding,
                    "argv_sha256": intent["argv_sha256"],
                },
            )
            return spawn(argv)
    except AdmissionError:
        raise
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        TypeError,
        subprocess.SubprocessError,
    ) as exc:
        raise AdmissionError("launch custody unavailable") from exc
