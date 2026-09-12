"""Authoritative governed coordination completion mutation."""

from __future__ import annotations

from pathlib import Path


def _completion_title(home: Path, task_id: str) -> str:
    """Require readable immutable identity before selecting completion policy."""
    from .ci_applicability import _json

    try:
        core = _json((home / "cards" / task_id / "core.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"card {task_id} has unreadable immutable core metadata") from None
    title = core.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError(f"card {task_id} has no valid immutable core title")
    return title


def complete_coord_task(home: Path, agent_name: str, task_id: str):
    """Validate governed review completion, then perform the board mutation."""
    from .coordination import Board
    from .review_verdict import validate_review_completion

    home_path = Path(home).expanduser()
    title = _completion_title(home_path, task_id)
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
        title = _completion_title(home_path, task_id)
        validate_review_completion(task_id, title, home_path)
    return transition_task(
        home_path,
        task_id=task_id,
        column=column,
        actor=agent_name,
        order=order,
    )
