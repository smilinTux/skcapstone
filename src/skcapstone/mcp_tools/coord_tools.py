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
            "Show the multi-agent coordination board. Lists all tasks with status, priority, "
            "and assignees. Shows active agents. Optional tag/parent/status filters bound "
            "the output (parent matches the 'parent-<id>' tag convention)."
        ),
        inputSchema={
            "properties": {
                "parent": {
                    "description": "Only tasks tagged 'parent-<id>' (children of this card)",
                    "type": "string",
                },
                "status": {
                    "description": "Only tasks in this status",
                    "enum": ["open", "claimed", "in_progress", "review", "done", "blocked"],
                    "type": "string",
                },
                "tag": {
                    "description": "Only tasks carrying this tag (repeatable)",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "limit": {
                    "description": "Bound the status payload to at most this many cards.",
                    "type": "integer",
                },
                "cursor": {
                    "description": "Opaque continuation cursor from a previous bounded call.",
                    "type": "string",
                },
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="coord_claim",
        description=(
            "Claim a task on the coordination board for an agent. Prevents duplicate work "
            "across agents. Refuses tasks whose dependencies are not all done unless "
            "force is true."
        ),
        inputSchema={
            "properties": {
                "agent_name": {"description": "Agent name claiming the task", "type": "string"},
                "task_id": {"description": "The task ID to claim", "type": "string"},
                "force": {
                    "description": "Claim even when dependencies are not all done",
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": ["task_id", "agent_name"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_complete",
        description="Mark a task as completed on the coordination board.",
        inputSchema={
            "properties": {
                "agent_name": {"description": "Agent name completing the task", "type": "string"},
                "task_id": {"description": "The task ID to complete", "type": "string"},
            },
            "required": ["task_id", "agent_name"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_create",
        description="Create a new task on the coordination board.",
        inputSchema={
            "properties": {
                "created_by": {"description": "Creator agent name", "type": "string"},
                "description": {"description": "Task description", "type": "string"},
                "priority": {
                    "description": "Task priority (default: medium)",
                    "enum": ["critical", "high", "medium", "low"],
                    "type": "string",
                },
                "tags": {"description": "Task tags", "items": {"type": "string"}, "type": "array"},
                "title": {"description": "Task title", "type": "string"},
            },
            "required": ["title"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_kanban",
        description=(
            "Show the unified kanban board over coord tasks and ITIL tickets: per-lane "
            "per-column counts, WIP status, and the active cards (ready/doing/review). "
            "Columns are the lifecycle; swimlanes are the card kind."
        ),
        inputSchema={"properties": {}, "required": [], "type": "object"},
    ),
    Tool(
        name="coord_move",
        description=(
            "Move a card to a kanban column (backlog/ready/doing/review/done). The explicit "
            "move is authoritative for the column."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "column": {
                    "description": "Target kanban column",
                    "enum": ["backlog", "ready", "doing", "review", "done"],
                    "type": "string",
                },
                "order": {"description": "Position within the column", "type": "integer"},
                "task_id": {"description": "The card/task ID", "type": "string"},
            },
            "required": ["task_id", "column"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_score",
        description=(
            "Record an autopilot grade on a coordination task. Appends to "
            "meta.autopilot.scores[] idempotently (same round+harness updates in place). "
            "Optionally sets phase and a pr/artifact ref."
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


async def _handle_coord_status(args: dict) -> list[TextContent]:
    """Return coordination board status, with optional tag/parent/status filters."""
    from ..coord_eligibility import leaf_eligibility_counts
    from ..coordination import Board

    board = Board(_home())
    views = board.get_task_views()
    agents = board.load_agents()

    tags = list(args.get("tag") or [])
    parent = args.get("parent")
    if parent:
        tags.append(f"parent-{parent}")
    if tags:
        wanted = {t.lower() for t in tags}
        views = [v for v in views if wanted & {t.lower() for t in v.task.tags}]
    status_filter = args.get("status")
    if status_filter:
        views = [v for v in views if v.status.value == status_filter]

    # Bounded payload + malformed-card report (SKCOORD-STATUS-BOUND-01).
    limit = args.get("limit")
    cursor = args.get("cursor")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
        return _error_response("limit must be a positive integer")

    try:
        from ..card_store import task_views_with_malformed

        _, malformed = task_views_with_malformed(_home())
    except ImportError:
        malformed = []
    for entry in malformed:
        logger.warning(
            "Malformed card %s (source %s): %s [evidence_sha256=%s]",
            entry["card_id"],
            entry["source"],
            entry["reason"],
            entry["evidence_sha256"],
        )

    bounded = limit is not None or cursor is not None
    all_views = views
    if bounded:
        if cursor is not None:
            views = [v for v in all_views if v.task.id > cursor]
            if limit is not None:
                views = views[:limit]
        elif limit is not None:
            views = all_views[:limit]
        has_more = limit is not None and len(views) < limit and cursor is None
        next_cursor = views[-1].task.id if (has_more and views) else None
    else:
        has_more = False
        next_cursor = None

    eligibility = leaf_eligibility_counts(_home(), {v.task.id for v in views})

    payload = {
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
        "malformed_cards": malformed,
        "has_more": has_more,
        "next_cursor": next_cursor,
        "summary": {
            "total": len(views),
            "open": sum(1 for v in views if v.status.value == "open"),
            "leaf_eligible": eligibility.leaves,
            "review_needs_identity": eligibility.review,
            "malformed": len(malformed),
            "claimed": sum(1 for v in views if v.status.value == "claimed"),
            "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
            "done": sum(1 for v in views if v.status.value == "done"),
        },
    }
    return _json_response(payload)


async def _handle_coord_claim(args: dict) -> list[TextContent]:
    """Claim a task on the board."""
    from ..coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_home())
    try:
        agent = board.claim_task(agent_name, task_id, force=bool(args.get("force", False)))
        return _json_response(
            {
                "claimed": True,
                "task_id": task_id,
                "agent": agent.agent,
                "current_task": agent.current_task,
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_coord_complete(args: dict) -> list[TextContent]:
    """Complete a task on the board."""
    from ..coordination import Board

    task_id = args.get("task_id", "")
    agent_name = args.get("agent_name", "")
    if not task_id or not agent_name:
        return _error_response("task_id and agent_name are required")

    board = Board(_home())
    # board.complete_task() automatically mints Joules via _mint_joules_for_task
    agent = board.complete_task(agent_name, task_id)

    # Report minted Joules in the response (best-effort)
    joules_minted = 0
    try:
        from ..coordination import _PRIORITY_JOULE_MAP

        for t in board.load_tasks():
            if t.id == task_id:
                _cat, _evt, joules_minted = _PRIORITY_JOULE_MAP.get(
                    t.priority.value, ("community", "support_ticket", 50)
                )
                break
    except Exception as exc:
        logger.warning("Failed to calculate joules for completed task %s: %s", task_id, exc)

    return _json_response(
        {
            "completed": True,
            "task_id": task_id,
            "agent": agent.agent,
            "completed_tasks": agent.completed_tasks,
            "joules_minted": joules_minted,
        }
    )


async def _handle_coord_create(args: dict) -> list[TextContent]:
    """Create a new task on the board."""
    from ..coordination import Board, Task, TaskPriority

    title = args.get("title", "")
    if not title:
        return _error_response("title is required")

    board = Board(_home())
    task = Task(
        title=title,
        description=args.get("description", ""),
        priority=TaskPriority(args.get("priority", "medium")),
        tags=args.get("tags", []),
        created_by=args.get("created_by", "mcp"),
    )
    path = board.create_task(task)
    return _json_response(
        {
            "created": True,
            "task_id": task.id,
            "title": task.title,
            "priority": task.priority.value,
            "path": str(path),
        }
    )


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
    return _json_response(
        {
            "scored": True,
            "task_id": task_id,
            "round": int(args["round"]),
            "score": int(args["score"]),
            "path": str(path),
        }
    )


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
    return _json_response(
        {
            "counts": counts,
            "wip": kb.wip_report(),
            "active": active,
            "totals": {
                "active": len(all_cards),
                "itil": sum(1 for c in all_cards if c.source == "itil"),
            },
        }
    )


async def _handle_coord_move(args: dict) -> list[TextContent]:
    """Move a card to a kanban column."""
    from skcoord.lifecycle import transition_task

    from ..card import Column

    task_id = args.get("task_id", "")
    column = args.get("column", "")
    if not task_id or not column:
        return _error_response("task_id and column are required")
    if column not in {c.value for c in Column}:
        return _error_response(f"invalid column '{column}'")

    try:
        receipt = transition_task(
            _shared_root(),
            task_id=task_id,
            column=column,
            actor=args.get("agent", "") or "coord-move",
            order=args.get("order"),
        )
    except (OSError, RuntimeError, ValueError) as exc:
        message = str(exc)
        if message == f"CardStore card {task_id} has no foldable core":
            message = f"Task {task_id} not found"
        return _error_response(message)
    return _json_response(
        {
            "moved": True,
            "task_id": task_id,
            "column": column,
            "projection_actions": list(receipt.actions),
        }
    )


# Tools present in this module but intentionally NOT published on the MCP
# wire surface (kept for direct import / tests). Excluded by
# collect_all_tools / collect_all_handlers.
HIDDEN: set[str] = {"coord_score"}

HANDLERS: dict = {
    "coord_status": _handle_coord_status,
    "coord_claim": _handle_coord_claim,
    "coord_complete": _handle_coord_complete,
    "coord_create": _handle_coord_create,
    "coord_kanban": _handle_coord_kanban,
    "coord_move": _handle_coord_move,
    "coord_score": _handle_coord_score,
}
