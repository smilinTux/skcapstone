"""Authoritative governed coordination completion mutation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatesPending:
    """Returned by complete_coord_task in place of completing the card.

    Not an error: a card with outstanding exit_gates is a normal, expected
    stopping point, not a failure. ``outstanding`` is the list of exit_gates
    entries (each a dict with at least a "gate" name and an "owner" seat)
    that have no matching gate_satisfied event yet.
    """

    task_id: str
    outstanding: list[dict]


def _read_core_exit_gates(home: Path, task_id: str) -> list[dict]:
    """Read exit_gates straight off core.json, never through CardCore.

    exit_gates exists on CardCore only on a sibling branch that is not
    installed here. The installed CardCore relies on pydantic's default
    extra="ignore", so it silently drops exit_gates on load, through
    CardStore.fold() or any other CardCore path. A card would look
    ungated to that path even when core.json plainly carries the field.
    Reading the raw JSON file is the only way to see it, so that is what
    this does. Do not "simplify" this into a CardCore/fold read.
    """
    core_path = home / "cards" / task_id / "core.json"
    if not core_path.exists():
        return []
    try:
        core = json.loads(core_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    gates = core.get("exit_gates")
    if not isinstance(gates, list):
        return []
    return [
        gate
        for gate in gates
        if isinstance(gate, dict) and isinstance(gate.get("gate"), str) and gate["gate"]
    ]


def _read_satisfied_gate_names(home: Path, task_id: str) -> set[str]:
    """Read gate_satisfied events straight off the card's event log."""
    events_dir = home / "cards" / task_id / "events"
    satisfied: set[str] = set()
    if not events_dir.exists():
        return satisfied
    for log in sorted(events_dir.glob("*.jsonl")):
        try:
            lines = log.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("action") != "gate_satisfied":
                continue
            name = event.get("gate")
            if isinstance(name, str) and name:
                satisfied.add(name)
    return satisfied


def outstanding_gates(home: Path, task_id: str) -> list[dict]:
    """Return the exit_gates entries with no matching gate_satisfied event."""
    home_path = Path(home).expanduser()
    gates = _read_core_exit_gates(home_path, task_id)
    if not gates:
        return []
    satisfied = _read_satisfied_gate_names(home_path, task_id)
    return [gate for gate in gates if gate["gate"] not in satisfied]


def satisfy_gate(home: Path, task_id: str, gate_name: str, agent_name: str) -> bool:
    """Record one exit gate as satisfied by its owning seat.

    Rejects a gate_name absent from the card's exit_gates, so a typo cannot
    silently satisfy nothing. Idempotent: satisfying an already-satisfied
    gate appends no duplicate event and returns False.

    Returns:
        True if a new gate_satisfied event was appended, False if the gate
        was already satisfied.
    """
    from .card_store import CardStore

    home_path = Path(home).expanduser()
    known_names = {gate["gate"] for gate in _read_core_exit_gates(home_path, task_id)}
    if gate_name not in known_names:
        raise ValueError(f"gate {gate_name!r} is not in exit_gates for {task_id}")
    if gate_name in _read_satisfied_gate_names(home_path, task_id):
        return False
    CardStore(home_path).append_event(task_id, "gate_satisfied", agent_name, gate=gate_name)
    return True


def complete_coord_task(home: Path, agent_name: str, task_id: str):
    """Validate governed review completion, then perform the board mutation.

    A card with outstanding exit_gates is not completed. An await_gates
    event is appended instead and a GatesPending is returned so the caller
    can report exactly which gates are outstanding and who owns each one.
    A card with no exit_gates at all, or one whose gates are all satisfied,
    completes exactly as before.
    """
    from .card_store import CardStore
    from .coordination import Board
    from .review_verdict import validate_review_completion

    home_path = Path(home).expanduser()
    title = ""
    core = home_path / "cards" / task_id / "core.json"
    if core.exists():
        try:
            title = str(json.loads(core.read_text()).get("title") or "")
        except (OSError, ValueError):
            title = ""
    validate_review_completion(task_id, title, home_path)

    pending = outstanding_gates(home_path, task_id)
    if pending:
        CardStore(home_path).append_event(
            task_id,
            "await_gates",
            agent_name,
            gates=[gate["gate"] for gate in pending],
        )
        return GatesPending(task_id=task_id, outstanding=pending)

    return Board(home_path).complete_task(agent_name, task_id)


def move_coord_task(
    home: Path,
    agent_name: str,
    task_id: str,
    column: str,
    order: int | None = None,
):
    """Validate a terminal move, then use the canonical lifecycle mutation.

    ``coord move <task_id> done`` reaches the board through this function
    instead of complete_coord_task, so it is a second entrypoint into the
    same terminal state and must obey the same exit_gates rule. A move to
    done with outstanding gates is refused with a ValueError naming the
    outstanding gates and their owners, rather than being silently
    converted into an await_gates card the way complete_coord_task
    converts it: move is a column operation, not a lifecycle assertion, and
    a caller scripting a move wants to know immediately that it did not
    happen, not discover later that it became something else. Use
    coord satisfy-gate for each outstanding gate, then move again.

    A card with no exit_gates, or one whose gates are all satisfied, moves
    exactly as before. A move to any column other than done is unaffected.
    """
    from skcoord.lifecycle import transition_task

    from .review_verdict import validate_review_completion

    home_path = Path(home).expanduser()
    if column == "done":
        title = ""
        core = home_path / "cards" / task_id / "core.json"
        if core.exists():
            try:
                title = str(json.loads(core.read_text()).get("title") or "")
            except (OSError, ValueError):
                title = ""
        validate_review_completion(task_id, title, home_path)
        pending = outstanding_gates(home_path, task_id)
        if pending:
            names = ", ".join(f"{gate['gate']} (owner: {gate.get('owner')})" for gate in pending)
            raise ValueError(
                f"task {task_id} has outstanding exit gates: {names}; "
                "run coord satisfy-gate for each before moving to done"
            )
    return transition_task(
        home_path,
        task_id=task_id,
        column=column,
        actor=agent_name,
        order=order,
    )
