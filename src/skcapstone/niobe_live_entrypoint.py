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

from .fleet_lane_health import MAX_SNAPSHOT_BYTES, active_gateway_revision, lane_health
from .niobe_activation import LIVE_UNIT, parse_activation
from .seat_mail import poll_mail, startup_hello

_LANES = {
    "codex": (
        "SKFLEET_TARGET",
        "SKFLEET_CODEX_LANE_MODEL",
        "sk-codex-mid",
        "SKFLEET_CODEX_CAPACITY_DOMAINS",
        "codex",
        3,
    ),
    "glm": (
        "SKFLEET_GLM_TARGET",
        "SKFLEET_GLM_MODEL",
        "sk-glm-s",
        "SKFLEET_GLM_CAPACITY_DOMAINS",
        "zai",
        0,
    ),
    "qwen": (
        "SKFLEET_QWEN_TARGET",
        "SKFLEET_QWEN_MODEL",
        "qwen3.8-27b-huihui-abliterated-q4_k_m",
        "SKFLEET_QWEN_CAPACITY_DOMAINS",
        "chiap01-qwen38,chiap08-qwen38",
        0,
    ),
    "kimi": (
        "SKFLEET_KIMI_TARGET",
        "SKFLEET_KIMI_MODEL",
        "kimi-for-coding",
        "SKFLEET_KIMI_CAPACITY_DOMAINS",
        "kimi-for-coding,kimi-k3",
        0,
    ),
}


def _validated_lane_targets(home: Path) -> dict[str, int]:
    """Return configured targets only when every active lane is exactly healthy."""

    targets: dict[str, int] = {}
    bindings: dict[str, tuple[str, tuple[str, ...]]] = {}
    for lane, (
        target_key,
        model_key,
        model_default,
        domains_key,
        domains_default,
        default,
    ) in _LANES.items():
        raw_target = os.environ.get(target_key, str(default))
        try:
            target = int(raw_target)
        except ValueError as exc:
            raise ValueError(f"Niobe {lane} target must be a nonnegative integer") from exc
        if target < 0 or str(target) != raw_target.strip():
            raise ValueError(f"Niobe {lane} target must be a nonnegative integer")
        domains = tuple(
            value.strip()
            for value in os.environ.get(domains_key, domains_default).split(",")
            if value.strip()
        )
        if target and not domains:
            raise ValueError(f"Niobe {lane} capacity domains are missing")
        targets[lane] = target
        bindings[lane] = (os.environ.get(model_key, model_default).strip(), domains)

    if not any(targets.values()):
        return targets
    snapshot_path = Path(
        os.environ.get(
            "SKFLEET_LANE_HEALTH_PATH",
            str(home / "evidence" / "fleet-lane-health.json"),
        )
    )
    try:
        raw = snapshot_path.read_bytes()
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("snapshot exceeds bound")
        snapshot = json.loads(raw)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Niobe lane health snapshot is unavailable") from exc
    if not isinstance(snapshot, dict) or snapshot.get("errors") != []:
        raise ValueError("Niobe lane health snapshot is invalid")
    endpoint = os.environ.get("SKFLEET_GATEWAY_URL", "http://chiap01:18790").rstrip("/")
    revision = active_gateway_revision(endpoint)
    cycle_id = str(snapshot.get("cycle_id") or "")
    for lane, target in targets.items():
        if not target:
            continue
        model, domains = bindings[lane]
        healthy, reason = lane_health(
            snapshot,
            lane,
            model,
            cycle_id=cycle_id,
            endpoint=endpoint,
            capacity_domains=domains,
            active_revision=revision,
        )
        if not healthy:
            raise ValueError(f"Niobe {lane} lane is not healthy: {reason}")
    return targets


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
    _validated_lane_targets(home)
    if not dispatcher.is_file():
        raise ValueError("Niobe dispatcher is missing")
    startup_hello(home, "niobe", host=host)
    mailbox = poll_mail("niobe")
    environment = os.environ.copy()
    environment["SKFLEET_NIOBE_ACTIVATION"] = str(activation_path.resolve())
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
