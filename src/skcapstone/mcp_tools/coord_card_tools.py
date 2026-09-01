"""Coordination card-hygiene tools: describe, label, link, reprioritize, amend-criteria.

The first three mirror the ``coord describe`` / ``coord label`` /
``coord link`` CLI verbs. Link uses SKCoord's canonical verified two-store
annotation primitive. ``coord_reprioritize`` and
``coord_amend_criteria`` are the folded amendment verbs (same discipline
as describe; see ``coord_amendments``), and ``coord_void`` kills a
mistaken card without completing it (no Joules, no changelog entry).
MCP-first agents can now do routine board hygiene without shelling out to
the CLI (cards 61b97e22, e78fd954, 325a737f).
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response, _shared_root

TOOLS: list[Tool] = [
    Tool(
        name="coord_describe",
        description=(
            "Edit a card's title and/or description (folded, never rewrites "
            "core.json). Only the fields passed are changed; an empty string "
            "clears a field. Same appended, writer-attributed event as the CLI."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "description": {"description": "New card description", "type": "string"},
                "task_id": {"description": "The card/task ID", "type": "string"},
                "title": {"description": "New card title", "type": "string"},
            },
            "required": ["task_id"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_label",
        description="Add (or remove) a label on a card.",
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "label": {"description": "The label to add or remove", "type": "string"},
                "remove": {
                    "description": "Remove the label instead of adding it (default: false)",
                    "type": "boolean",
                },
                "task_id": {"description": "The card/task ID", "type": "string"},
            },
            "required": ["task_id", "label"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_link",
        description="Attach a link (pr/commit/doc/...) to a card.",
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "key": {"description": "Link key (e.g. 'pr', 'commit', 'doc')", "type": "string"},
                "task_id": {"description": "The card/task ID", "type": "string"},
                "value": {"description": "Link value (URL or ref)", "type": "string"},
            },
            "required": ["task_id", "key", "value"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_reprioritize",
        description=(
            "Amend a card's priority (folded, never rewrites core.json). The amendment "
            "is one appended, writer-attributed event, reversed by reprioritizing again."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "priority": {
                    "description": "New priority for the card",
                    "enum": ["critical", "high", "medium", "low"],
                    "type": "string",
                },
                "task_id": {"description": "The card/task ID", "type": "string"},
            },
            "required": ["task_id", "priority"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_amend_criteria",
        description=(
            "Replace a card's acceptance criteria (folded, never rewrites core.json). "
            "The event carries the full replacement list; latest event wins."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "criteria": {
                    "description": "Full replacement acceptance criteria list",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "type": "array",
                },
                "task_id": {"description": "The card/task ID", "type": "string"},
            },
            "required": ["task_id", "criteria"],
            "type": "object",
        },
    ),
    Tool(
        name="coord_void",
        description=(
            "Void a mistakenly created card WITHOUT completing it: appends a "
            "writer-attributed void event and archives the card. It leaves the active "
            "board, mints no Joules, stays out of the changelog, and remains foldable "
            "for audit."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Writer name (defaults to host)", "type": "string"},
                "reason": {
                    "description": "Why the card is being voided (required for audit)",
                    "type": "string",
                },
                "task_id": {"description": "The card/task ID", "type": "string"},
            },
            "required": ["task_id", "reason"],
            "type": "object",
        },
    ),
]


async def _handle_coord_describe(args: dict) -> list[TextContent]:
    """Edit a card's title/description via one appended overlay event."""
    from ..card import CardEvent, CardEventLog

    task_id = args.get("task_id", "")
    if not task_id:
        return _error_response("task_id is required")
    title = args.get("title")
    description = args.get("description")
    if title is None and description is None:
        return _error_response("title and/or description are required")

    home = _shared_root()
    agent = args.get("agent", "") or ""
    try:
        CardEventLog(home).append(
            CardEvent(
                card_id=task_id,
                action="describe",
                title=title,
                description=description,
                writer=agent,
            )
        )
        from ..card_store import card_store_write_enabled, mirror_coord_describe

        if card_store_write_enabled():
            mirror_coord_describe(home, task_id, agent, title=title, description=description)
    except ValueError as exc:
        return _error_response(str(exc))
    changed = [k for k, v in (("title", title), ("description", description)) if v is not None]
    return _json_response({"described": True, "task_id": task_id, "changed": changed})


async def _handle_coord_label(args: dict) -> list[TextContent]:
    """Add or remove a label on a card via one appended overlay event."""
    from ..card import CardEvent, CardEventLog

    task_id = args.get("task_id", "")
    label = args.get("label", "")
    if not task_id or not label:
        return _error_response("task_id and label are required")

    remove = bool(args.get("remove", False))
    action = "remove_label" if remove else "add_label"
    CardEventLog(_shared_root()).append(
        CardEvent(card_id=task_id, action=action, label=label, writer=args.get("agent", "") or "")
    )
    return _json_response({"labeled": True, "task_id": task_id, "label": label, "action": action})


async def _handle_coord_link(args: dict) -> list[TextContent]:
    """Attach a link through the verified authoritative and overlay primitive."""
    from ..blocked_verdict import validate_blocked_verdict
    from ..coord_receipts import verified_coord_link

    task_id = args.get("task_id", "")
    key = args.get("key", "")
    value = args.get("value", "")
    if not task_id:
        return _error_response("task_id, key, and value are required")

    try:
        validate_blocked_verdict(key, value)
        receipt = verified_coord_link(
            _shared_root(), task_id, key, value, args.get("agent", "") or ""
        )
    except (ValueError, RuntimeError) as exc:
        return _error_response(str(exc))
    return _json_response(receipt)


async def _handle_coord_reprioritize(args: dict) -> list[TextContent]:
    """Amend a card's priority via the folded set_priority event."""
    from ..coord_amendments import reprioritize

    task_id = args.get("task_id", "")
    priority = args.get("priority", "")
    if not task_id or not priority:
        return _error_response("task_id and priority are required")
    try:
        reprioritize(_shared_root(), task_id, priority, args.get("agent", "") or "")
    except ValueError as exc:
        return _error_response(str(exc))
    return _json_response({"reprioritized": True, "task_id": task_id, "priority": priority})


async def _handle_coord_amend_criteria(args: dict) -> list[TextContent]:
    """Replace a card's acceptance criteria via one appended store event."""
    from ..coord_amendments import amend_criteria, current_acceptance_criteria

    task_id = args.get("task_id", "")
    criteria = args.get("criteria") or []
    if not task_id or not criteria:
        return _error_response("task_id and at least one criterion are required")

    home = _shared_root()
    try:
        amend_criteria(home, task_id, list(criteria), args.get("agent", "") or "")
        folded = current_acceptance_criteria(home, task_id)
    except ValueError as exc:
        return _error_response(str(exc))
    return _json_response(
        {
            "amended": True,
            "task_id": task_id,
            "acceptance_criteria": folded,
        }
    )


async def _handle_coord_void(args: dict) -> list[TextContent]:
    """Void a mistakenly created card without completing it."""
    from ..coord_amendments import void_card

    task_id = args.get("task_id", "")
    reason = args.get("reason", "")
    if not task_id or not reason:
        return _error_response("task_id and reason are required")
    try:
        void_card(_shared_root(), task_id, reason, args.get("agent", "") or "")
    except ValueError as exc:
        return _error_response(str(exc))
    return _json_response({"voided": True, "task_id": task_id, "reason": reason})


HANDLERS: dict = {
    "coord_describe": _handle_coord_describe,
    "coord_label": _handle_coord_label,
    "coord_link": _handle_coord_link,
    "coord_reprioritize": _handle_coord_reprioritize,
    "coord_amend_criteria": _handle_coord_amend_criteria,
    "coord_void": _handle_coord_void,
}
