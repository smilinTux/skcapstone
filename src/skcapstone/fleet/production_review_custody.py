"""Retain production review custody and verify attributable terminal workers."""

import os
import re
import socket
import subprocess
from pathlib import Path

from .production_review_evidence import ReviewEvidenceError
from .production_review_finish import once, read_json


def exit_path(home, card, claim):
    """Name one immutable terminal observation for one exact review claim."""
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{32}", claim):
        raise ReviewEvidenceError("terminal review identity invalid")
    return Path(home) / "evidence/production-review-exits" / (card + "-" + claim + ".json")


def retain_review_exit(home, args, card, *, terminal_proven):
    """Never route production review success into legacy release-to-backlog."""
    state = {
        "state": "awaiting-review-acceptance",
        "claim_released": False,
        "process_terminal": terminal_proven,
    }
    try:
        if not terminal_proven:
            raise ReviewEvidenceError("exact process and cgroup death unproven")
        if card.owner != args.owner or card.meta.get("_claim_revision") != args.claim_revision:
            raise ReviewEvidenceError("review claim changed")
        invocation = os.environ.get("INVOCATION_ID", "")
        if not re.fullmatch(r"[0-9a-f]{32}", invocation):
            raise ReviewEvidenceError("managed review invocation missing")
        unit = "skfleet-worker-" + args.lane + "-" + args.card + ".service"
        groups = Path("/proc/self/cgroup").read_text().splitlines()
        if not any(line.rsplit("/", 1)[-1] == unit for line in groups):
            raise ReviewEvidenceError("managed review unit differs")
        if type(getattr(args, "production_child_exit_code", None)) is not int:
            raise ReviewEvidenceError("review child terminal status missing")
        if args.host != socket.gethostname().split(".")[0].lower():
            raise ReviewEvidenceError("review host differs")
        receipt = {
            "schema": "skfleet.production-review-exit/v1",
            "card": args.card,
            "owner": args.owner,
            "claim_revision": args.claim_revision,
            "host": args.host,
            "lane": args.lane,
            "model": args.model,
            "unit": unit,
            "invocation": invocation,
            "workspace": str(Path.cwd().resolve()),
            "source_head": args.source_base_revision,
            "exit_code": args.production_child_exit_code,
        }
        path = exit_path(home, args.card, args.claim_revision)
        once(path, receipt)
        state["terminal_receipt"] = str(path)
    except (OSError, ValueError, AttributeError):
        state["reason"] = "exact-review-terminal-receipt-pending"
    args.production_source_disposition = state
    return state


def unit_terminal(unit, invocation, *, host=None):
    """Recheck a recorded terminal invocation; collected units may be absent.

    Callers must first verify the native terminal observation for this exact
    claim. A missing unit alone never proves that any worker ran or succeeded.
    """
    if not re.fullmatch(
        r"skfleet-(?:worker|builder)-[A-Za-z0-9_.-]+\.service", unit
    ) or not re.fullmatch(r"[0-9a-f]{32}", str(invocation)):
        raise ReviewEvidenceError("worker unit identity invalid")
    command = [
        "systemctl",
        "--user",
        "show",
        unit,
        "--property=LoadState,ActiveState,SubState,MainPID,ControlPID,InvocationID,Result,ExecMainStatus",
    ]
    if host is not None and host != socket.gethostname().split(".")[0].lower():
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,252}", host):
            raise ReviewEvidenceError("worker host invalid")
        command = [
            "ssh",
            "-oBatchMode=yes",
            "-oStrictHostKeyChecking=yes",
            "-oConnectTimeout=5",
            host,
            *command,
        ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (
        result.returncode in {0, 4}
        and values.get("LoadState") == "not-found"
        and values.get("ActiveState") == "inactive"
        and values.get("MainPID") == "0"
        and values.get("ControlPID") == "0"
    ):
        return values
    if (
        result.returncode
        or values.get("InvocationID") != invocation
        or values.get("LoadState") != "loaded"
        or values.get("ActiveState") not in {"inactive", "failed"}
        or values.get("SubState") not in {"dead", "failed"}
        or values.get("MainPID") != "0"
        or values.get("ControlPID") != "0"
    ):
        raise ReviewEvidenceError("exact managed worker death unproven")
    return values


def read_exit(home, card, claim):
    """Load immutable managed review exit evidence, never a model's assertion."""
    value = read_json(exit_path(home, card, claim))
    if (
        value.get("schema") != "skfleet.production-review-exit/v1"
        or value.get("card") != card
        or value.get("claim_revision") != claim
        or value.get("exit_code") != 0
    ):
        raise ReviewEvidenceError("review exit did not succeed")
    return value
