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
        request_id = os.environ.get("SKFLEET_REVIEW_REQUEST")
        if request_id:
            from . import builder_dispatch
            from .paths import FleetPaths

            node = os.environ.get("SKFLEET_REVIEW_NODE", "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", node) or ".." in node:
                raise ReviewEvidenceError("remote review node missing")
            paths = FleetPaths(Path(home) / "fleet")
            status = read_json(builder_dispatch.status_path(paths, node, args.card))
            execution = status.get("execution", {})
            if (
                status.get("request_id") != request_id
                or status.get("owner") != args.owner
                or status.get("claim_revision") != args.claim_revision
                or execution.get("invocation") != invocation
                or execution.get("unit") != unit
                or execution.get("host") != args.host
            ):
                raise ReviewEvidenceError("remote review execution acknowledgment pending")
            receipt["execution"] = execution
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


def import_remote_exits(home, policy):
    """Retain only exact destination terminal evidence validated by authority."""
    from skcoord.card_store import CardStore

    from .production_receipts import production_receipt_allowed

    for path in (Path(home) / "fleet/status").glob("*/dispatch/*.json"):
        try:
            status = read_json(path)
            if (
                status.get("work_kind") != "review"
                or status.get("state")
                not in {"awaiting-review-acceptance", "review-fail", "review-blocked"}
                or status.get("node") != path.parent.parent.name
            ):
                continue
            terminal, execution = status["terminal"], status["execution"]
            card = CardStore(home).fold(status["card_id"])
            events = [
                event
                for event in CardStore(home)._read_events(card.id)
                if event.get("action") == "review_assignment_launch"
                and event.get("claim_revision") == status["claim_revision"]
            ]
            launch = dict(
                host=execution["host"],
                owner=status["owner"],
                revision=status["claim_revision"],
                lane=terminal["lane"],
                model=terminal["model"],
            )
            if (
                len(events) != 1
                or terminal.get("execution") != execution
                or terminal.get("card") != card.id
                or terminal.get("owner") != card.owner
                or terminal.get("claim_revision") != launch["revision"]
                or any(
                    terminal.get(key) != execution[key] for key in ("host", "unit", "invocation")
                )
                or not production_receipt_allowed(home, policy, launch, card, events[0])
            ):
                continue
            unit_terminal(execution["unit"], execution["invocation"], host=execution["host"])
            once(exit_path(home, card.id, launch["revision"]), terminal)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue  # Missing or delayed sync retains existing custody.
