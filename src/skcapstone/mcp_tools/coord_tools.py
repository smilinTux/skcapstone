"""Coordination board tools."""

from __future__ import annotations

import logging

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response, _shared_root

logger = logging.getLogger(__name__)

TOOLS: list[Tool] = [
    Tool(
        name="coord_status",
        description=(
            "Show the multi-agent coordination board. Lists all tasks "
            "with status, priority, and assignees. Shows active agents."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="coord_claim",
        description=(
            "Claim a task on the coordination board for an agent. "
            "Prevents duplicate work across agents."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to claim",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent name claiming the task",
                },
            },
            "required": ["task_id", "agent_name"],
        },
    ),
    Tool(
        name="coord_complete",
        description=(
            "Mark a task as completed on the coordination board."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to complete",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent name completing the task",
                },
            },
            "required": ["task_id", "agent_name"],
        },
    ),
    Tool(
        name="coord_create",
        description=(
            "Create a new task on the coordination board."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Task title",
                },
                "description": {
                    "type": "string",
                    "description": "Task description",
                },
                "priority": {
                    "type": "string",
                    "enum": ["critical", "high", "medium", "low"],
                    "description": "Task priority (default: medium)",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Task tags",
                },
                "created_by": {
                    "type": "string",
                    "description": "Creator agent name",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="coord_kanban",
        description=(
            "Show the unified kanban board over coord tasks and ITIL tickets. "
            "Returns per-lane per-column counts, WIP status, and the active "
            "cards (ready/doing/review). Columns are the lifecycle; swimlanes "
            "are the card kind (feature/bug/security/expedite/change/problem)."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="coord_move",
        description=(
            "Move a card to a kanban column (backlog/ready/doing/review/done). "
            "The explicit move is authoritative for the card's column."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The card/task ID to move"},
                "column": {
                    "type": "string",
                    "enum": ["backlog", "ready", "doing", "review", "done"],
                    "description": "Target kanban column",
                },
                "order": {"type": "integer", "description": "Position within the column"},
                "agent": {"type": "string", "description": "Writer name (defaults to host)"},
            },
            "required": ["task_id", "column"],
        },
    ),
    Tool(
        name="coord_score",
        description=(
            "Record an autopilot grade on a coordination task. Appends to "
            "meta.autopilot.scores[] idempotently (same round+harness updates "
            "in place). Optionally sets phase and a pr/artifact ref."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task ID to score"},
                "round": {"type": "integer", "description": "Grading round number"},
                "score": {"type": "integer", "description": "Score value (rubric 1-5)"},
                "notes": {"type": "string", "description": "Grader notes"},
                "harness": {"type": "string", "description": "Harness / grader identity"},
                "phase": {"type": "string", "description": "Autopilot phase label"},
                "ref": {"type": "string", "description": "PR URL (http*) or artifact ref"},
            },
            "required": ["task_id", "round", "score"],
        },
    ),
]


async def _handle_coord_status(_args: dict) -> list[TextContent]:
    """Return coordination board status."""
    from ..coordination import Board

    board = Board(_shared_root())
    views = board.get_task_views()
    agents = board.load_agents()

    return _json_response({
        "tasks": [
            {
                "id": v.task.id,
                "title": v.task.title,
                "priority": v.task.priority.value,
                "status": v.status.value,
                "claimed_by": v.claimed_by,
                "tags": v.task.tags,
                "description": v.task.description[:150] if v.task.description else "",
            }
            for v in views
        ],
        "agents": [
            {
                "name": a.agent,
                "state": a.state.value,
                "current_task": a.current_task,
                "claimed": a.claimed_tasks,
                "completed_count": len(a.completed_tasks),
            }
            for a in agents
        ],
        "summary": {
            "total": len(views),
            "open": sum(1 for v in views if v.status.value == "open"),
            "claimed": sum(1 for v in views if v.status.value == "claimed"),
            "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
            "done": sum(1 for v in views if v.status.value == "done"),
        },
    })


async def _handle_coord_claim(args: dict) -> list[TextContent]:
    """Claim a task on the board."""
    from ..coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_shared_root())
    try:
        agent = board.claim_task(agent_name, task_id)
        try:
            from .. import activity
            activity.push("task.claimed", {"task_id": task_id, "agent": agent_name})
        except Exception as exc:
            logger.warning("Failed to push task.claimed activity for %s: %s", task_id, exc)
        return _json_response({
            "claimed": True,
            "task_id": task_id,
            "agent": agent.agent,
            "current_task": agent.current_task,
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_coord_complete(args: dict) -> list[TextContent]:
    """Complete a task on the board."""
    from ..coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_shared_root())
    agent = board.complete_task(agent_name, task_id)
    try:
        from .. import activity
        activity.push("task.completed", {"task_id": task_id, "agent": agent_name})
    except Exception as exc:
        logger.warning("Failed to push task.completed activity for %s: %s", task_id, exc)
    return _json_response({
        "completed": True,
        "task_id": task_id,
        "agent": agent.agent,
        "completed_tasks": agent.completed_tasks,
    })


async def _handle_coord_create(args: dict) -> list[TextContent]:
    """Create a new task on the board."""
    from ..coordination import Board, Task, TaskPriority

    title = args.get("title", "")
    if not title:
        return _error_response("title is required")

    board = Board(_shared_root())
    task = Task(
        title=title,
        description=args.get("description", ""),
        priority=TaskPriority(args.get("priority", "medium")),
        tags=args.get("tags", []),
        created_by=args.get("created_by", "mcp"),
    )
    path = board.create_task(task)
    return _json_response({
        "created": True,
        "task_id": task.id,
        "title": task.title,
        "priority": task.priority.value,
        "path": str(path),
    })


async def _handle_coord_score(args: dict) -> list[TextContent]:
    """Record an autopilot grade on a task."""
    from ..coordination import Board

    task_id = args.get("task_id", "")
    if not task_id or "round" not in args or "score" not in args:
        return _error_response("task_id, round, and score are required")

    board = Board(_shared_root())
    try:
        path = board.score_task(
            task_id,
            round=int(args["round"]),
            score=int(args["score"]),
            notes=args.get("notes", ""),
            harness=args.get("harness", ""),
            phase=args.get("phase"),
            ref=args.get("ref"),
        )
    except FileNotFoundError as exc:
        return _error_response(str(exc))
    return _json_response({
        "scored": True,
        "task_id": task_id,
        "round": int(args["round"]),
        "score": int(args["score"]),
        "path": str(path),
    })


async def _handle_coord_kanban(_args: dict) -> list[TextContent]:
    """Return the unified kanban board state."""
    from ..card import COLUMN_ORDER, LANE_ORDER, KanbanBoard

    kb = KanbanBoard(_shared_root())
    grid = kb.grid()
    counts = {
        lane: {col: len(grid[lane][col]) for col in COLUMN_ORDER}
        for lane in LANE_ORDER
        if any(grid[lane][col] for col in COLUMN_ORDER)
    }
    active = [
        {
            "id": c.id,
            "title": c.title,
            "kind": c.kind.value,
            "status": c.status.value,
            "swimlane": c.swimlane,
            "priority": c.priority,
            "owner": c.owner,
        }
        for lane in LANE_ORDER
        for col in ("ready", "doing", "review")
        for c in grid[lane][col]
    ]
    all_cards = kb.cards()
    return _json_response({
        "counts": counts,
        "wip": kb.wip_report(),
        "active": active,
        "totals": {
            "active": len(all_cards),
            "itil": sum(1 for c in all_cards if c.source == "itil"),
        },
    })


async def _handle_coord_move(args: dict) -> list[TextContent]:
    """Move a card to a kanban column."""
    from ..card import CardEvent, CardEventLog, Column

    task_id = args.get("task_id", "")
    column = args.get("column", "")
    if not task_id or not column:
        return _error_response("task_id and column are required")
    if column not in {c.value for c in Column}:
        return _error_response(f"invalid column '{column}'")

    root = _shared_root()
    CardEventLog(root).append(
        CardEvent(
            card_id=task_id,
            action="move",
            column=column,
            order=args.get("order"),
            writer=args.get("agent", "") or "",
        )
    )
    try:
        from ..card_store import card_store_write_enabled, mirror_coord_move

        if card_store_write_enabled():
            mirror_coord_move(root, task_id, column, args.get("agent", "") or "",
                              order=args.get("order"))
    except Exception:  # noqa: BLE001
        pass
    return _json_response({"moved": True, "task_id": task_id, "column": column})


HANDLERS: dict = {
    "coord_status": _handle_coord_status,
    "coord_claim": _handle_coord_claim,
    "coord_complete": _handle_coord_complete,
    "coord_create": _handle_coord_create,
    "coord_kanban": _handle_coord_kanban,
    "coord_move": _handle_coord_move,
    "coord_score": _handle_coord_score,
}
