"""Exact process-generation checks and atomic native liveness publication."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from skcoord.card_store import CardStore, card_mutation_lock

UNIT = re.compile(r"skfleet-worker-[a-z]+-([0-9a-f]{8})\.service")


def unit_properties(unit: str, runner: Callable) -> dict[str, str]:
    """Read one unit's process identity and retained wrapper command."""
    result = runner(
        [
            "systemctl",
            "--user",
            "show",
            unit,
            "--property=ExecMainPID,InvocationID,ExecStart,ControlGroup,ActiveState,WorkingDirectory",
        ]
    )
    return (
        dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode == 0
        else {}
    )


def matches_process(
    properties: dict[str, str], *, card: str, owner: str, claim: str, pid: int, invocation: str
) -> bool:
    """Bind a beat to systemd's exact invocation and wrapper arguments.

    ExecStart survives process exit. Only the wrapper prefix before its ``--``
    child-command delimiter is interpreted; shell text is never executed.
    """
    if (
        pid <= 0
        or properties.get("ExecMainPID") != str(pid)
        or not re.fullmatch(r"[0-9a-f]{32}", invocation)
        or properties.get("InvocationID") != invocation
    ):
        return False
    command = properties.get("ExecStart", "")
    if command.count("argv[]=") != 1:
        return False
    prefix, separator, _ = command.split("argv[]=", 1)[1].partition(" -- ")
    if not separator:
        return False
    try:
        argv = shlex.split(prefix)
    except ValueError:
        return False
    if len(argv) < 2 or Path(argv[1]).name != "skfleet-worker-wrapper.py":
        return False
    for flag, expected in (("--card", card), ("--owner", owner), ("--claim-revision", claim)):
        if argv.count(flag) != 1:
            return False
        index = argv.index(flag) + 1
        if index >= len(argv) or argv[index] != expected:
            return False
    return True


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Bound host observation without invoking a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)


def publish_liveness(
    home: Path,
    *,
    card: str,
    owner: str,
    claim: str,
    state: str,
    unit: str,
    pid: int,
    invocation: str,
    actor: str,
    runner: Callable = _run,
) -> bool:
    """Append only a changed, current observation while holding the card lock.

    Lifecycle attribution stays with the observer. The expected worker owner
    is a fence, never an impersonated writer. Stale observations are no-ops.
    """
    from skcapstone.jarvis_emergency import authorize_coord_mutation
    from skcapstone.seat_boundaries import Action

    authorize_coord_mutation(actor, Action.LINK_CARD, card, None, None)
    match = UNIT.fullmatch(unit)
    if not match or match.group(1) != card or state not in {"active", "terminal"}:
        raise ValueError("invalid liveness projection")
    if not owner or not claim or "|" in owner or "|" in claim:
        raise ValueError("missing or invalid worker generation")
    with card_mutation_lock(home, card):
        store = CardStore(home)
        current = store.fold(card)
        if (
            current is None
            or current.status not in {"claimed", "doing", "ready", "review"}
            or current.owner != owner
            or current.meta.get("_claim_revision") != claim
        ):
            return False
        properties = unit_properties(unit, runner)
        if not matches_process(
            properties, card=card, owner=owner, claim=claim, pid=pid, invocation=invocation
        ):
            return False
        if properties.get("ActiveState") != ("active" if state == "active" else "inactive"):
            return False
        value = f"{owner}|{claim}|{state}"
        if current.links.get("worker_liveness") == value:
            return False
        store.append_event(
            card,
            "link",
            actor,
            link_key="worker_liveness",
            link_value=value,
            expected_owner=owner,
            expected_claim_revision=claim,
            process_pid=pid,
            process_invocation=invocation,
            process_unit=unit,
        )
        return True
