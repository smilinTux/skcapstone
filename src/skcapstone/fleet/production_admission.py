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
from .production_resources import active_resource_units, local_worker_admission
from .production_test_plan import private_dir, read_json, write_once

MARKER = "SKFLEET_ADMISSION_ID"


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
            "--property=Id,LoadState,ActiveState,InvocationID,Environment,MemoryMax,MemoryCurrent"
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
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise AdmissionError("admission lock is not private")
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield root


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


def _occupancy(root: Path, home: Path) -> list[dict]:
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
        if (directory / "failed-terminal.json").exists():
            proof = read_json(directory / "failed-terminal.json")
            if not _valid_failed_receipt(proof, intent):
                raise AdmissionError("failed launch terminal receipt is inconsistent")
            # Any current service remains charged by the live inventory above.
            continue
        if (directory / "observed.json").exists():
            observed = read_json(directory / "observed.json")
            if (
                observed.get("reservation_id") != directory.name
                or observed.get("unit") != unit
                or observed.get("memory_max_bytes") != maximum
                or not re.fullmatch(r"[0-9a-f]{32}", observed.get("invocation", ""))
            ):
                raise AdmissionError("reservation acknowledgment is inconsistent")
            continue
        if unit in units:
            state = unit_state(unit)
            if state.get("Id") != unit:
                raise AdmissionError("pending service response is inconsistent")
            if state.get("ActiveState") in {"inactive", "failed"}:
                units[unit]["reserved_memory_max"] = maximum
                continue
            if (
                state.get("Id") != unit
                or state.get("LoadState") != "loaded"
                or state.get("ActiveState") not in {"active", "activating"}
                or state.get(MARKER) != directory.name
                or int(state.get("MemoryMax", "0")) != maximum
                or int(state.get("MemoryCurrent", "-1")) < 0
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


def _valid_failed_receipt(proof: dict, intent: dict) -> bool:
    """Bind durable terminal evidence to every original source and command byte."""
    return (
        proof.get("schema") == "skfleet.failed-admission-terminal/v1"
        and proof.get("intent_sha256") == _digest(intent)
        and proof.get("reservation_id") == _reservation_id(intent)
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(proof.get("card_revision", ""))))
        and proof.get("process_absent") is True
        and proof.get("cgroup_empty") is True
        and isinstance(proof.get("state"), dict)
        and _failed_state(proof["state"], intent, proof["state"].get("InvocationID", ""))
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
    """Finalize only a retained failed invocation under unchanged native custody.

    The caller supplies its exact original command and source binding. No launch
    is replayed, unit stopped, claim released or absent intent aged out. A lost
    reply may reread the same immutable receipt while that exact claim remains.
    """
    intent = _intent(policy, host, unit, binding, argv)
    identity = _reservation_id(intent)
    if not re.fullmatch(r"[0-9a-f]{8}", str(binding.get("card_id", ""))):
        raise AdmissionError("failed launch card identity invalid")
    with _transaction(home, host) as root, card_mutation_lock(home, binding["card_id"]):
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
        path = directory / "failed-terminal.json"
        if path.exists():
            proof = read_json(path)
            if (
                not _valid_failed_receipt(proof, intent)
                or proof["state"]["InvocationID"] != invocation
                or proof["card_revision"] != expected_card_revision
            ):
                raise AdmissionError("failed launch receipt changed")
            return proof
        if (directory / "observed.json").exists():
            raise AdmissionError("launch already observed; normal lifecycle owns termination")
        before = unit_state(unit, terminal=True)
        if not _failed_state(before, intent, invocation):
            raise AdmissionError("exact retained failed invocation unavailable")
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
            schema="skfleet.failed-admission-terminal/v1",
            reservation_id=identity,
            intent_sha256=_digest(intent),
            card_revision=expected_card_revision,
            state=before,
            process_absent=True,
            cgroup_empty=True,
        )
        if not _valid_failed_receipt(proof, intent):
            raise AdmissionError("failed launch terminal proof is invalid")
        write_once(path, proof)
        return proof


def reserve_launch(
    home: Path, policy: dict, host: str, unit: str, binding: dict, argv: list[str]
) -> list[str]:
    """Reserve before spawning and return an argv bearing the exact intent marker.

    Callers validate the current claim before this call and retain custody on
    every exception after it. Spawn and lifecycle waits happen after the lock
    has closed. No caller may replay an existing intent. Operators use this same
    API only after all installed launch paths qualify for the shared protocol.
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
        limits = policy["node_quotas"][host]
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
        with _transaction(home, host) as root:
            directory = root / identity
            if directory.exists():
                raise AdmissionError("launch already reserved; exact custody required")
            rows = _occupancy(root, home)
            if any(row["unit"] == unit for row in rows):
                raise AdmissionError("unit already occupied or pending")
            ready, reason = local_worker_admission(policy, host, rows)
            if not ready:
                if reason.startswith("memory_available="):
                    raise AdmissionDeferredError(reason)
                raise AdmissionError(reason)
            private_dir(directory, create=True)
            write_once(directory / "intent.json", intent)
        return [argv[0], "--setenv=" + MARKER + "=" + identity, *argv[1:]]
    except AdmissionError:
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        raise AdmissionError("node-resource-evidence-unavailable") from exc
