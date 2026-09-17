"""Authoritative governed coordination completion mutation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# Exit code coord.py's CLI complete command uses when the outcome is
# GatesPending, and the exact value the fleet's review closer checks for to
# tell a gated card apart from a real close. Never 0 (a caller that only
# checks returncode == 0 must not mistake gated for completed) and never 1
# (already the CLI's plain-error exit code). Shared here so the CLI and the
# fleet dispatcher, which both act on this return value, cannot drift apart.
GATED_EXIT_CODE = 3


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


#: The signal this codebase already uses to mean "this card is code work
#: bound to a repository". execute_mux._card_routing (src/skcapstone/
#: execute_mux.py) reads exactly this prefix on a folded card's labels to
#: route it to the sandboxed code bridge instead of the comms dispatcher, so
#: reusing it here means a card the rest of the system already treats as
#: code work is exactly the card this gate treats as code work too. No
#: second, divergent heuristic is invented for this one call site.
_REPO_LABEL_PREFIX = "repo:"

#: The evidence link this gate requires. `commit` (a neighbouring, older
#: link key with 100 uses on the live board) is a free-form human prose
#: field, and only 4 of those 100 uses are an actual 40-hex SHA. It cannot
#: be machine-read, so it must never be accepted here.
_COMMIT_SHA_LINK_KEY = "commit_sha"


def _card_labels_and_links(home: Path, task_id: str) -> tuple[list, dict]:
    """Return a card's folded (labels, links), or ([], {}) on any failure.

    Fails closed toward NOT blocking: this backs a refusal check, so a fold
    error here must never start refusing completions across the board. A
    genuinely missing or unreadable card is already caught earlier in
    complete_coord_task, through validate_review_completion and the board
    mutation itself, so this path is only reached for a card that resolved
    fine moments earlier.
    """
    try:
        from .card_store import CardStore

        card = CardStore(Path(home).expanduser()).fold(task_id)
    except Exception:  # noqa: BLE001 - this check must never crash a completion
        return [], {}
    if card is None:
        return [], {}
    labels = list(getattr(card, "labels", None) or [])
    links = dict(getattr(card, "links", None) or {})
    return labels, links


def _missing_commit_evidence(home: Path, task_id: str) -> bool:
    """True when a repo-labeled card has no non-blank commit_sha link.

    The literal value "none" satisfies this exactly like a real SHA: this
    checks only that the link is present and non-blank, never what it says,
    because the worker prompt (scripts/fleet/skfleet-rotate.py,
    _worker_done_instructions) tells a worker to link commit_sha to the
    literal value "none" when a card needed no repository change, and that
    recorded decision must complete in one command, not be blocked by it.
    """
    labels, links = _card_labels_and_links(home, task_id)
    touches_repository = any(str(label).lower().startswith(_REPO_LABEL_PREFIX) for label in labels)
    if not touches_repository:
        return False
    commit_sha = str(links.get(_COMMIT_SHA_LINK_KEY) or "").strip()
    return not commit_sha


def complete_coord_task(home: Path, agent_name: str, task_id: str):
    """Validate governed review completion, then perform the board mutation.

    A card with outstanding exit_gates is not completed. An await_gates
    event is appended instead and a GatesPending is returned so the caller
    can report exactly which gates are outstanding and who owns each one.
    A card with no exit_gates at all, or one whose gates are all satisfied,
    completes exactly as before.

    A `repo:<name>`-labeled card (see _REPO_LABEL_PREFIX) with no
    commit_sha link is refused the same way, reusing GatesPending and
    GATED_EXIT_CODE rather than a second refusal mechanism: coord.py's CLI
    complete command and the fleet's close_reviewed_parents already branch
    on exactly this shape and need no changes to also understand this
    refusal. Unlike the exit_gates branch, this one does NOT append an
    await_gates event: that event has no matching core.json exit_gates
    entry for "commit_sha", and the fleet dispatcher's own state fold
    (scripts/fleet/skfleet-rotate.py, around line 2260) treats an
    await_gates event against an empty declared-gates set as a stale or
    malformed read that fails closed forever, never auto-clearing. Writing
    that event here would permanently misclassify the card as
    "awaiting-gates" in the fleet even after commit_sha is linked. This
    check is cheap to re-run instead: complete_coord_task re-derives the
    commit_sha presence fresh from the card's links on every call, so no
    event needs to remember anything.
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

    if _missing_commit_evidence(home_path, task_id):
        return GatesPending(
            task_id=task_id,
            outstanding=[
                {
                    "gate": _COMMIT_SHA_LINK_KEY,
                    "owner": agent_name,
                    "message": (
                        f"card {task_id} is labeled repo:* (touches a repository) but "
                        "has no commit_sha link; run: skcapstone coord link "
                        f"{task_id} commit_sha <the 40-character SHA, or the literal "
                        "value none if no repository change was needed>"
                    ),
                }
            ],
        )

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
        # Same gate as complete_coord_task. Gating only completion would leave
        # move-to-done as an unlocked side door, which reads as enforcement
        # while providing none. That exact bypass already had to be closed once
        # for exit gates; this is the same door for commit evidence.
        if _missing_commit_evidence(home_path, task_id):
            raise ValueError(
                f"task {task_id} touches a repository and has no commit_sha "
                f"link; run: skcapstone coord link {task_id} commit_sha "
                "<the 40-character SHA, or the literal none if the card needed "
                "no repository change> before moving to done"
            )
    return transition_task(
        home_path,
        task_id=task_id,
        column=column,
        actor=agent_name,
        order=order,
    )
