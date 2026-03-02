"""Coordination board tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response, _shared_root

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


HANDLERS: dict = {
    "coord_status": _handle_coord_status,
    "coord_claim": _handle_coord_claim,
    "coord_complete": _handle_coord_complete,
    "coord_create": _handle_coord_create,
}
