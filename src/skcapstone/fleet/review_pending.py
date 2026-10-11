"""Proofs for an unacknowledged review admission, never a launched review."""

import re
import socket
import subprocess

from . import production_admission as admission
from . import production_builder


def status_matches(status, request, owner, claim, host, unit):
    """Bind the pending status and reservation to the exact released offer."""
    if not isinstance(status, dict):
        return False
    execution = status.get("execution")
    intent = status.get("admission")
    if not isinstance(execution, dict) or not isinstance(intent, dict):
        return False
    binding = intent.get("binding", {})
    if not isinstance(binding, dict) or not all(k in intent for k in ("host", "unit", "binding")):
        return False
    return bool(
        status.get("schema") == "skfleet.builder-dispatch-status/v1"
        and status.get("state") == "admission-pending"
        and status.get("work_kind") == "review"
        and all(status.get(k) == request.get(k) for k in ("card_id", "node", "request_id"))
        and status.get("owner") == owner == request.get("reviewer")
        and status.get("claim_revision") == claim
        and re.fullmatch(r"[0-9a-f]{32}", str(claim))
        and status.get("review_packet") is None
        and status.get("terminal") is None
        and status.get("acknowledgment") is None
        and execution.get("invocation") == ""
        and execution.get("host") == host == request.get("production", {}).get("host")
        and execution.get("node") == request.get("node")
        and execution.get("unit") == unit == intent.get("unit")
        and execution.get("work_kind") == "review"
        and execution.get("request_id") == request.get("request_id")
        and execution.get("request_sha256") == production_builder.digest(request)
        and execution.get("admission_id") == admission._reservation_id(intent)
        and execution.get("admission_sha256") == admission._digest(intent)
        and intent.get("schema") == "skfleet.resource-admission/v1"
        and intent.get("host") == host
        and all(
            binding.get(k) == v
            for k, v in dict(
                card_id=request.get("card_id"),
                owner=owner,
                claim_revision=claim,
                request_id=request.get("request_id"),
                work_kind="review",
            ).items()
        )
    )


def inactive(state):
    """Require a fully stopped reserved unit, including both process IDs."""
    return (
        state.get("LoadState") in {"loaded", "not-found"}
        and state.get("ActiveState") in {"inactive", "failed"}
        and state.get("SubState") in {"dead", "failed"}
        and state.get("MainPID") == "0"
        and state.get("ControlPID") == "0"
    )


def unit_state(unit, host):
    """Query the destination strictly; an unavailable host never proves absence."""
    if not re.fullmatch(r"skfleet-worker-[a-zA-Z0-9_.-]+\.service", unit) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{0,252}", host
    ):
        raise ValueError("pending review destination invalid")
    command = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,InvocationID",
    ]
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
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (
        result.returncode not in {0, 4}
        or (result.returncode == 4 and state.get("LoadState") != "not-found")
        or not inactive(state)
    ):
        raise ValueError("pending review reserved unit inactivity unproven")
    return state
