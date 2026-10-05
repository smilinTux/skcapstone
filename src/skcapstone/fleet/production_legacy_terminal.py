"""Recover collected legacy assignments without inventing missing start receipts."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from datetime import datetime
from pathlib import Path

from skcoord.card_store import CardStore

from . import production_admission as admission
from .production_test_plan import read_json, write_once

START_ID = "39f53479d3a045ac8e11786248231fbf"


def _launches(home: Path, intent: dict) -> list[dict]:
    """Identify only native launches of the exact legacy worker claim."""
    binding = intent["binding"]
    match = re.fullmatch(
        r"skfleet-worker-(codex|glm|deepseek)-([0-9a-f]{8})\.service", intent["unit"]
    )
    if (
        not match
        or match[2] != binding["card_id"]
        or binding["owner"] != f"pi-{match[1]}-{intent['host']}-{match[2]}"
    ):
        return []
    return [
        event
        for event in CardStore(home)._read_events(binding["card_id"])
        if event.get("action") == "production_assignment_launch"
        and event.get("schema") == "skfleet.production-assignment-launch/v1"
        and event.get("launched") is True
        and event.get("worker") == binding["owner"]
        and event.get("writer") == binding["owner"]
        and event.get("node") == intent["host"]
        and event.get("claim_revision") == binding["claim_revision"]
    ]


def _fenced_claim(directory: Path, home: Path, intent: dict, *, current=True) -> list[dict]:
    """A consumed claim fence is evidence, never a synthetic launch event."""
    binding = intent["binding"]
    match = re.fullmatch(
        r"skfleet-worker-(codex|glm|deepseek)-([0-9a-f]{8})\.service", intent["unit"]
    )
    if (
        not match
        or match[2] != binding["card_id"]
        or binding["owner"] != f"pi-{match[1]}-{intent['host']}-{match[2]}"
        or not (directory / "start.json").exists()
        or read_json(directory / "start.json")
        != dict(
            schema="skfleet.resource-start/v1",
            reservation_id=admission._reservation_id(intent),
            binding=binding,
            argv_sha256=intent["argv_sha256"],
        )
        or read_json(directory / "fenced-start-required.json")
        != dict(reservation_id=admission._reservation_id(intent), claim_fenced=True)
    ):
        return []
    store = CardStore(home)
    if current:
        card = store.fold(binding["card_id"])
        if (
            card is None
            or card.archived
            or card.meta.get("claim_conflicts")
            or card.status.value != "doing"
            or card.owner != binding["owner"]
            or card.meta.get("_claim_revision") != binding["claim_revision"]
        ):
        
    return []
    return [
        event
        for event in store._read_events(binding["card_id"])
        if event.get("action") == "claim"
        and event.get("writer") == binding["owner"]
        and event.get("owner") == binding["owner"]
        and event.get("node") == intent["host"]
        and event.get("claim_revision") == binding["claim_revision"]
    ]


def _wrapper_binding(entry: dict, intent: dict) -> bool:
    """Read only manager-owned description arguments before the shell payload."""
    prefix = "Started " + intent["unit"] + " - "
    message = entry.get("MESSAGE", "")
    if not message.startswith(prefix) or " -- " not in message:
        return False
    try:
        words = shlex.split(message[len(prefix) :].split(" -- ", 1)[0])
        if len(words) < 2 or Path(words[1]).name != "skfleet-worker-wrapper.py":
            return False
        expected = {
            "--card": intent["binding"]["card_id"],
            "--owner": intent["binding"]["owner"],
            "--claim-revision": intent["binding"]["claim_revision"],
            "--host": intent["host"],
            "--lane": intent["unit"].split("-")[2],
        }
        return all(
            words.count(key) == 1
            and not any(word.startswith(key + "=") for word in words)
            and words[words.index(key) + 1] == value
            for key, value in expected.items()
        )
    except (ValueError, IndexError, TypeError):
        return False


def _launch_time(event: dict) -> int:
    """Convert a native timestamp, refusing unqualified local time."""
    stamp = datetime.fromisoformat(event["ts"])
    if stamp.tzinfo is None:
        raise ValueError("launch timestamp lacks timezone")
    return int(stamp.timestamp() * 1_000_000)


def _valid(proof: dict, intent: dict, launches: list[dict], *, fenced=False) -> bool:
    """Bind cached terminal evidence to the immutable native launch generation."""
    try:
        if len(launches) != 1:
            return False
        start = int(proof["start_realtime_us"])
        terminal = int(proof["terminal_realtime_us"])
        anchor = _launch_time(launches[0])
        return (
            proof.get("schema")
            == (
                "skfleet.fenced-assignment-terminal/v1"
                if fenced
                else "skfleet.legacy-assignment-terminal/v1"
            )
            and proof.get("intent_sha256") == admission._digest(intent)
            and proof.get("reservation_id") == admission._reservation_id(intent)
            and proof.get("unit") == intent["unit"]
            and proof.get("claim_event_sha256" if fenced else "launch_event_sha256")
            == admission._digest(launches[0])
            and bool(re.fullmatch(r"[0-9a-f]{32}", str(proof.get("invocation", ""))))
            and (
                anchor <= start <= anchor + intent["resources"]["runtime_max_seconds"] * 1_000_000
                if fenced
                else abs(start - anchor) <= 2_000_000
            )
            and start
            < terminal
            <= start + (intent["resources"]["runtime_max_seconds"] + 60) * 1_000_000
            and proof.get("terminal_message_id") in admission._JOURNAL_TERMINAL_IDS
            and all(
                re.fullmatch(r"[0-9a-f]{64}", str(proof.get(key, "")))
                for key in ("start_entry_sha256", "terminal_entry_sha256")
            )
        )
    except (KeyError, TypeError, ValueError):
        return False


def _absent(state: dict, unit: str) -> bool:
    """Require a collected service with no remaining process or cgroup."""
    return (
        state.get("Id") == unit
        and state.get("LoadState") == "not-found"
        and state.get("ActiveState") == "inactive"
        and state.get("SubState") == "dead"
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
        and state.get("ControlGroup") == ""
        and state.get("TasksCurrent") in {"0", "[not set]"}
        and state.get("InvocationID") == ""
    )


def reconcile_legacy_assignment(
    directory: Path, home: Path, intent: dict, *, live: bool
) -> bool | None:
    """Return terminal, still charged, or not a native legacy assignment.

    This writes only a distinct admission terminal receipt. Native claims,
    candidate bytes, start acknowledgements and source custody are untouched.
    A launched assignment is never passed to prestart reconciliation.
    """
    launches = _launches(home, intent)
    fenced = not launches
    path = directory / (
        "fenced-assignment-terminal.json" if fenced else "legacy-assignment-terminal.json"
    )
    if fenced:
        # Historical resource discharge survives later legitimate card handoff.
        # A fresh recovery still requires the unchanged current native claim.
        launches = _fenced_claim(directory, home, intent, current=not path.exists())

    def valid(proof):
        return _valid(proof, intent, launches, fenced=fenced) and (
            not fenced
            or proof.get("start_receipt_sha256")
            == admission._digest(read_json(directory / "start.json"))
        )

    if path.exists():
        if not valid(read_json(path)):
            raise admission.AdmissionError("legacy assignment terminal proof changed")
        return True
    if not launches:
        return None
    if live or len(launches) != 1:
        return False
    state = admission.unit_state(intent["unit"], terminal=True)
    if not _absent(state, intent["unit"]):
        return False
    try:
        when = _launch_time(launches[0])
        result = subprocess.run(
            [
                "journalctl",
                "--user",
                "--quiet",
                "--no-pager",
                "--output=json",
                "_COMM=systemd",
                "USER_UNIT=" + intent["unit"],
                "MESSAGE_ID=" + START_ID,
                *["MESSAGE_ID=" + message for message in sorted(admission._JOURNAL_TERMINAL_IDS)],
                *(
                    []
                    if fenced
                    else [
                        "--since=@" + str(when // 1_000_000 - 2),
                        "--until=@"
                        + str(when // 1_000_000 + intent["resources"]["runtime_max_seconds"] + 62),
                    ]
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode or len(result.stdout.encode()) > 65536:
            return False
        rows = [
            (json.loads(line), hashlib.sha256(line.encode()).hexdigest())
            for line in result.stdout.splitlines()
        ]
        if not rows or any(
            row.get("USER_UNIT") != intent["unit"]
            or not re.fullmatch(r"[0-9a-f]{32}", str(row.get("USER_INVOCATION_ID", "")))
            for row, digest in rows
        ):
            return False
        invocations = {row["USER_INVOCATION_ID"] for row, digest in rows}
        starts = [(row, digest) for row, digest in rows if row.get("MESSAGE_ID") == START_ID]
        terminals = [
            (row, digest)
            for row, digest in rows
            if row.get("MESSAGE_ID") in admission._JOURNAL_TERMINAL_IDS
        ]
        if len(invocations) != 1 or len(starts) != 1 or not terminals:
            return False
        start, start_digest = starts[0]
        if fenced and not _wrapper_binding(start, intent):
            return False
        terminal, terminal_digest = max(
            terminals, key=lambda entry: int(entry[0]["__REALTIME_TIMESTAMP"])
        )
        proof = dict(
            schema=(
                "skfleet.fenced-assignment-terminal/v1"
                if fenced
                else "skfleet.legacy-assignment-terminal/v1"
            ),
            intent_sha256=admission._digest(intent),
            reservation_id=admission._reservation_id(intent),
            unit=intent["unit"],
            invocation=start["USER_INVOCATION_ID"],
            start_realtime_us=start["__REALTIME_TIMESTAMP"],
            terminal_realtime_us=terminal["__REALTIME_TIMESTAMP"],
            terminal_message_id=terminal["MESSAGE_ID"],
            start_entry_sha256=start_digest,
            terminal_entry_sha256=terminal_digest,
        )
        proof["claim_event_sha256" if fenced else "launch_event_sha256"] = admission._digest(
            launches[0]
        )
        if fenced:
            proof["start_receipt_sha256"] = admission._digest(read_json(directory / "start.json"))
        if (
            not valid(proof)
            or admission.unit_state(intent["unit"], terminal=True) != state
            or (_fenced_claim(directory, home, intent) if fenced else _launches(home, intent))
            != launches
        ):
            return False
    except (OSError, KeyError, TypeError, ValueError, subprocess.SubprocessError):
        return False
    write_once(path, proof)
    return True
