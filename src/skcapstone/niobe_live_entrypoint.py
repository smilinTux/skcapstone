"""Fail-closed Niobe wrapper for the existing fleet dispatcher."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .niobe_activation import LIVE_UNIT, parse_activation
from .seat_mail import poll_mail, startup_hello


def _append_health(
    home: Path,
    *,
    host: str,
    returncode: int,
    mailbox: object,
    activation: object,
    started_at: str,
    exception_type: str | None = None,
    cycle_id: str,
    evidence_path: Path | None = None,
    outcome: str | None = None,
) -> None:
    """Append one truthful receipt for every live dispatcher invocation."""

    path = home / "coordination" / "seat-cycles" / "niobe.health.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at,
        "cycle_id": cycle_id,
        "seat": "niobe",
        "host": host,
        "result": outcome or ("dispatch_failed" if returncode else "bounded_noop"),
        "reason": None if returncode == 0 else f"dispatcher_exit_{returncode}",
        "dispatcher_returncode": returncode,
        "exception_type": exception_type,
        "activation_decision": activation.decision_id,
        "activation_card_revision": activation.card_revision,
        "unit": LIVE_UNIT,
        "invocation_id": os.environ.get("INVOCATION_ID", ""),
        "rotation_evidence_path": None,
        "rotation_evidence_sha256": None,
        "launches": 0,
        "launch_failures": 0,
        "suppressions": 0,
        "slot_summary": None,
        **mailbox.as_dict(),
    }
    if evidence_path and evidence_path.is_file():
        lines = evidence_path.read_text(encoding="utf-8", errors="replace").splitlines()
        payload.update(
            {
                "rotation_evidence_path": str(evidence_path),
                "rotation_evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                "launches": sum(line.startswith("LAUNCHED|") for line in lines),
                "launch_failures": sum(line.startswith("LAUNCH_FAILED|") for line in lines),
                "suppressions": sum(
                    "WITHHELD|" in line or "BLOCKED|" in line or "UNSUPPORTED_CARD_ID|" in line
                    for line in lines
                ),
                "slot_summary": next((line for line in lines if line.startswith("SLOTS|")), None),
            }
        )
    with path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        if any(json.loads(line).get("cycle_id") == cycle_id for line in stream if line.strip()):
            return
        stream.seek(0, os.SEEK_END)
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_live(
    *,
    activation_path: Path,
    dispatcher: Path,
    local_host: str | None = None,
    runner=subprocess.run,
) -> int:
    value = json.loads(activation_path.read_text(encoding="utf-8"))
    home = activation_path.parent.parent
    host = (local_host or socket.gethostname()).strip().lower()
    # parse_activation proves the card fence and that this machine is the host
    # the record authorizes, using the estate's own operator and product scope.
    activation = parse_activation(value, home=home, host=host)
    started_at = datetime.now(timezone.utc).isoformat()
    required_environment = {
        "SKFLEET_TARGET": "3",
        "SKFLEET_QWEN_TARGET": "0",
        "SKFLEET_GLM_TARGET": "0",
        "SKFLEET_KIMI_TARGET": "0",
    }
    if any(os.environ.get(key) != expected for key, expected in required_environment.items()):
        raise ValueError("Niobe live unit requires effective Codex-only target 3")
    if not dispatcher.is_file():
        raise ValueError("Niobe dispatcher is missing")
    startup_hello(home, "niobe", host=host)
    mailbox = poll_mail("niobe")
    environment = os.environ.copy()
    environment["SKFLEET_NIOBE_ACTIVATION"] = str(activation_path.resolve())
    environment["SKFLEET_ROTATION_HOSTS"] = host
    invocation = os.environ.get("INVOCATION_ID", "").lower()
    cycle_id = (
        invocation
        if len(invocation) == 32
        and all(character in "0123456789abcdef" for character in invocation)
        else hashlib.sha256(f"{activation.card_revision}:{started_at}".encode()).hexdigest()[:32]
    )
    environment["SKFLEET_ROTATION_ID"] = cycle_id
    evidence_path = home / "evidence" / "fleet-rotation" / cycle_id / "actions.log"
    try:
        completed = runner(
            [sys.executable, str(dispatcher), "--go"],
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _append_health(
            home,
            host=host,
            returncode=70,
            mailbox=mailbox,
            activation=activation,
            started_at=started_at,
            exception_type=type(exc).__name__,
            cycle_id=cycle_id,
            evidence_path=evidence_path,
        )
        raise
    returncode = int(completed.returncode)
    outcome = None
    if returncode == 0:
        if not evidence_path.is_file():
            returncode = 70
            outcome = "dispatch_failed"
        else:
            lines = evidence_path.read_text(encoding="utf-8", errors="replace").splitlines()
            final = next((line for line in reversed(lines) if line.strip()), "")
            if final.startswith("CYCLE_RECEIPT|") and "|launched=0|" not in final:
                outcome = "launch"
            elif final.startswith("CYCLE_RECEIPT|") or final.startswith("NOOP_RECEIPT|"):
                outcome = "bounded_noop"
            else:
                returncode = 70
                outcome = "dispatch_failed"
    _append_health(
        home,
        host=host,
        returncode=returncode,
        mailbox=mailbox,
        activation=activation,
        started_at=started_at,
        cycle_id=cycle_id,
        evidence_path=evidence_path,
        outcome=outcome,
    )
    return returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--activation", type=Path, required=True)
    parser.add_argument("--dispatcher", type=Path, required=True)
    parser.add_argument("--unit", default=LIVE_UNIT)
    args = parser.parse_args(argv)
    if args.unit != LIVE_UNIT:
        raise ValueError("Niobe live unit name does not match the decision")
    return run_live(activation_path=args.activation, dispatcher=args.dispatcher)


if __name__ == "__main__":
    raise SystemExit(main())
