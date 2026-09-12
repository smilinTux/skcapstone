"""Truthful read-only projection of historical agent heartbeat records."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STALE_AFTER_SECONDS = 15 * 60


def project_claim_revision(
    home: Path, agent_name: str, task_id: str, expected_revision: str | None
) -> bool:
    """Atomically add one exact current claim generation to agent evidence."""
    from skcoord.card_store import CardStore, card_mutation_lock
    from skcoord.coordination import Board, _board_mutation_lock

    if not expected_revision:
        return False
    board = Board(home)
    with _board_mutation_lock(home), card_mutation_lock(home, task_id):
        card = CardStore(home).fold(task_id)
        if (
            card is None
            or card.owner != agent_name
            or card.meta.get("_claim_revision") != expected_revision
        ):
            return False
        path = board.agent_projection_path(agent_name)
        try:
            projection = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if (
            not isinstance(projection, dict)
            or projection.get("agent") != agent_name
            or projection.get("current_task") != task_id
        ):
            return False
        projection["_claim_revision"] = expected_revision
        board._replace_agent_projection_bytes(
            agent_name, (json.dumps(projection, indent=2) + "\n").encode("utf-8")
        )
        return True


def display_state(agent, *, now: datetime | None = None) -> str:
    """Return display liveness without rewriting the historical agent record."""

    if agent.state.value == "offline":
        return "offline"
    try:
        observed = datetime.fromisoformat(agent.last_seen.replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
    except (AttributeError, TypeError, ValueError):
        return "stale"
    if age < 0 or age > STALE_AFTER_SECONDS:
        return "stale"
    return "active" if agent.current_task else "idle"
