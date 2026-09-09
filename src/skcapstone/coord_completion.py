"""Authoritative governed coordination completion mutation."""

from __future__ import annotations

import json
from pathlib import Path


def complete_coord_task(home: Path, agent_name: str, task_id: str):
    """Validate governed review completion, then perform the board mutation."""
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
    return Board(home_path).complete_task(agent_name, task_id)


def move_coord_task(
    home: Path,
    agent_name: str,
    task_id: str,
    column: str,
    order: int | None = None,
):
    """Validate a terminal move, then use the canonical lifecycle mutation."""
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
    return transition_task(
        home_path,
        task_id=task_id,
        column=column,
        actor=agent_name,
        order=order,
    )
