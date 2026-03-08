"""ITIL service management tools — Incident, Problem, Change, KEDB."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response, _shared_root

# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS: list[Tool] = [
    Tool(
        name="itil_incident_create",
        description=(
            "Create a new ITIL incident for a service disruption. "
            "Auto-creates a linked GTD item (next-action for sev1/sev2, inbox for sev3/sev4)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Brief description of the incident",
                },
                "severity": {
                    "type": "string",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "description": "Severity level (default: sev3)",
                },
                "source": {
                    "type": "string",
                    "enum": [
                        "service_health", "dreaming", "manual",
                        "daemon_error", "heartbeat",
                    ],
                    "description": "Detection source (default: manual)",
                },
                "affected_services": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of affected service names",
                },
                "impact": {
                    "type": "string",
                    "description": "Business impact description",
                },
                "managed_by": {
                    "type": "string",
                    "description": "Agent responsible for managing this incident",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="itil_incident_update",
        description=(
            "Update an incident: transition status, escalate severity, "
            "add timeline notes, or resolve. Valid status transitions: "
            "detected->acknowledged->investigating->resolved->closed "
            "(escalated branches off at any open state)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "incident_id": {
                    "type": "string",
                    "description": "Incident ID (e.g. inc-a1b2c3d4)",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent making the update",
                },
                "new_status": {
                    "type": "string",
                    "enum": [
                        "acknowledged", "investigating", "escalated",
                        "resolved", "closed",
                    ],
                    "description": "New status to transition to",
                },
                "severity": {
                    "type": "string",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "description": "New severity (for escalation/de-escalation)",
                },
                "note": {
                    "type": "string",
                    "description": "Timeline note",
                },
                "resolution_summary": {
                    "type": "string",
                    "description": "Resolution summary (when resolving)",
                },
                "related_problem_id": {
                    "type": "string",
                    "description": "Link to a related problem record",
                },
            },
            "required": ["incident_id", "agent"],
        },
    ),
    Tool(
        name="itil_incident_list",
        description=(
            "List ITIL incidents filtered by status, severity, or affected service."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "detected", "acknowledged", "investigating",
                        "escalated", "resolved", "closed",
                    ],
                    "description": "Filter by status",
                },
                "severity": {
                    "type": "string",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "description": "Filter by severity",
                },
                "service": {
                    "type": "string",
                    "description": "Filter by affected service name",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="itil_problem_create",
        description=(
            "Create a new ITIL problem record to investigate root cause. "
            "Links to related incidents and auto-creates a GTD project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Problem title",
                },
                "managed_by": {
                    "type": "string",
                    "description": "Agent responsible for investigation",
                },
                "related_incident_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Related incident IDs",
                },
                "workaround": {
                    "type": "string",
                    "description": "Known workaround if any",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="itil_problem_update",
        description=(
            "Update a problem record: transition status, set root cause, "
            "add workaround, optionally create a KEDB entry. "
            "Valid transitions: identified->analyzing->known_error->resolved."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "problem_id": {
                    "type": "string",
                    "description": "Problem ID (e.g. prb-e5f6g7h8)",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent making the update",
                },
                "new_status": {
                    "type": "string",
                    "enum": ["analyzing", "known_error", "resolved"],
                    "description": "New status to transition to",
                },
                "root_cause": {
                    "type": "string",
                    "description": "Root cause description",
                },
                "workaround": {
                    "type": "string",
                    "description": "Workaround description",
                },
                "note": {
                    "type": "string",
                    "description": "Timeline note",
                },
                "create_kedb": {
                    "type": "boolean",
                    "description": "Create a KEDB entry from this problem (default: false)",
                },
            },
            "required": ["problem_id", "agent"],
        },
    ),
    Tool(
        name="itil_change_propose",
        description=(
            "Propose a change (RFC). Standard changes auto-approve. "
            "Normal changes require CAB approval. Emergency changes have a "
            "15-min timeout before auto-approval."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Change title",
                },
                "change_type": {
                    "type": "string",
                    "enum": ["standard", "normal", "emergency"],
                    "description": "Type of change (default: normal)",
                },
                "risk": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Risk level (default: medium)",
                },
                "rollback_plan": {
                    "type": "string",
                    "description": "How to roll back if the change fails",
                },
                "test_plan": {
                    "type": "string",
                    "description": "How to verify the change works",
                },
                "managed_by": {
                    "type": "string",
                    "description": "Agent managing the change",
                },
                "implementer": {
                    "type": "string",
                    "description": "Agent who will implement the change",
                },
                "related_problem_id": {
                    "type": "string",
                    "description": "Related problem ID if applicable",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags for categorization",
                },
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="itil_change_update",
        description=(
            "Update a change: transition status (implementing, deployed, "
            "verified, failed, closed) or add timeline notes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "change_id": {
                    "type": "string",
                    "description": "Change ID (e.g. chg-i1j2k3l4)",
                },
                "agent": {
                    "type": "string",
                    "description": "Agent making the update",
                },
                "new_status": {
                    "type": "string",
                    "enum": [
                        "reviewing", "approved", "rejected", "implementing",
                        "deployed", "verified", "failed", "closed",
                    ],
                    "description": "New status to transition to",
                },
                "note": {
                    "type": "string",
                    "description": "Timeline note",
                },
            },
            "required": ["change_id", "agent"],
        },
    ),
    Tool(
        name="itil_cab_vote",
        description=(
            "Submit a CAB (Change Advisory Board) vote for a proposed change. "
            "Each agent writes its own vote file (conflict-free). "
            "A human rejection blocks the change; a human approval unblocks it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "change_id": {
                    "type": "string",
                    "description": "Change ID to vote on",
                },
                "agent": {
                    "type": "string",
                    "description": "Voting agent name",
                },
                "decision": {
                    "type": "string",
                    "enum": ["approved", "rejected", "abstain"],
                    "description": "Vote decision (default: abstain)",
                },
                "conditions": {
                    "type": "string",
                    "description": "Conditions for approval (e.g. 'deploy during maintenance window')",
                },
            },
            "required": ["change_id", "agent"],
        },
    ),
    Tool(
        name="itil_status",
        description=(
            "ITIL dashboard: open incidents by severity, active problems, "
            "pending changes, and KEDB count."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="itil_kedb_search",
        description=(
            "Search the Known Error Database by symptoms, service name, "
            "or keywords. Returns matching entries with workarounds."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (matches title, symptoms, root cause, tags)",
                },
            },
            "required": ["query"],
        },
    ),
]


# ═══════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════


async def _handle_itil_incident_create(args: dict) -> list[TextContent]:
    """Create a new incident."""
    from ..itil import ITILManager

    title = args.get("title", "").strip()
    if not title:
        return _error_response("title is required")

    mgr = ITILManager(_shared_root())
    incident = mgr.create_incident(
        title=title,
        severity=args.get("severity", "sev3"),
        source=args.get("source", "manual"),
        affected_services=args.get("affected_services", []),
        impact=args.get("impact", ""),
        managed_by=args.get("managed_by", ""),
        tags=args.get("tags", []),
    )
    return _json_response({
        "created": True,
        "id": incident.id,
        "title": incident.title,
        "severity": incident.severity.value,
        "status": incident.status.value,
        "managed_by": incident.managed_by,
        "gtd_item_ids": incident.gtd_item_ids,
    })


async def _handle_itil_incident_update(args: dict) -> list[TextContent]:
    """Update an incident."""
    from ..itil import ITILManager

    incident_id = args.get("incident_id", "").strip()
    agent = args.get("agent", "").strip()
    if not incident_id or not agent:
        return _error_response("incident_id and agent are required")

    mgr = ITILManager(_shared_root())
    try:
        inc = mgr.update_incident(
            incident_id=incident_id,
            agent=agent,
            new_status=args.get("new_status"),
            severity=args.get("severity"),
            note=args.get("note", ""),
            resolution_summary=args.get("resolution_summary"),
            related_problem_id=args.get("related_problem_id"),
        )
        return _json_response({
            "updated": True,
            "id": inc.id,
            "title": inc.title,
            "severity": inc.severity.value,
            "status": inc.status.value,
            "timeline_count": len(inc.timeline),
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_itil_incident_list(args: dict) -> list[TextContent]:
    """List incidents with optional filters."""
    from ..itil import ITILManager

    mgr = ITILManager(_shared_root())
    incidents = mgr.list_incidents(
        status=args.get("status"),
        severity=args.get("severity"),
        service=args.get("service"),
    )
    return _json_response({
        "incidents": [
            {
                "id": i.id,
                "title": i.title,
                "severity": i.severity.value,
                "status": i.status.value,
                "managed_by": i.managed_by,
                "affected_services": i.affected_services,
                "detected_at": i.detected_at,
                "resolved_at": i.resolved_at,
            }
            for i in incidents
        ],
        "total": len(incidents),
    })


async def _handle_itil_problem_create(args: dict) -> list[TextContent]:
    """Create a new problem."""
    from ..itil import ITILManager

    title = args.get("title", "").strip()
    if not title:
        return _error_response("title is required")

    mgr = ITILManager(_shared_root())
    problem = mgr.create_problem(
        title=title,
        managed_by=args.get("managed_by", ""),
        related_incident_ids=args.get("related_incident_ids", []),
        workaround=args.get("workaround", ""),
        tags=args.get("tags", []),
    )
    return _json_response({
        "created": True,
        "id": problem.id,
        "title": problem.title,
        "status": problem.status.value,
        "managed_by": problem.managed_by,
        "related_incident_ids": problem.related_incident_ids,
    })


async def _handle_itil_problem_update(args: dict) -> list[TextContent]:
    """Update a problem."""
    from ..itil import ITILManager

    problem_id = args.get("problem_id", "").strip()
    agent = args.get("agent", "").strip()
    if not problem_id or not agent:
        return _error_response("problem_id and agent are required")

    mgr = ITILManager(_shared_root())
    try:
        prb = mgr.update_problem(
            problem_id=problem_id,
            agent=agent,
            new_status=args.get("new_status"),
            root_cause=args.get("root_cause"),
            workaround=args.get("workaround"),
            note=args.get("note", ""),
            create_kedb=args.get("create_kedb", False),
        )
        return _json_response({
            "updated": True,
            "id": prb.id,
            "title": prb.title,
            "status": prb.status.value,
            "root_cause": prb.root_cause,
            "kedb_id": prb.kedb_id,
            "timeline_count": len(prb.timeline),
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_itil_change_propose(args: dict) -> list[TextContent]:
    """Propose a change (RFC)."""
    from ..itil import ITILManager

    title = args.get("title", "").strip()
    if not title:
        return _error_response("title is required")

    mgr = ITILManager(_shared_root())
    change = mgr.propose_change(
        title=title,
        change_type=args.get("change_type", "normal"),
        risk=args.get("risk", "medium"),
        rollback_plan=args.get("rollback_plan", ""),
        test_plan=args.get("test_plan", ""),
        managed_by=args.get("managed_by", ""),
        implementer=args.get("implementer"),
        related_problem_id=args.get("related_problem_id"),
        tags=args.get("tags", []),
    )
    return _json_response({
        "created": True,
        "id": change.id,
        "title": change.title,
        "change_type": change.change_type.value,
        "status": change.status.value,
        "cab_required": change.cab_required,
        "managed_by": change.managed_by,
    })


async def _handle_itil_change_update(args: dict) -> list[TextContent]:
    """Update a change status."""
    from ..itil import ITILManager

    change_id = args.get("change_id", "").strip()
    agent = args.get("agent", "").strip()
    if not change_id or not agent:
        return _error_response("change_id and agent are required")

    mgr = ITILManager(_shared_root())
    try:
        chg = mgr.update_change(
            change_id=change_id,
            agent=agent,
            new_status=args.get("new_status"),
            note=args.get("note", ""),
        )
        return _json_response({
            "updated": True,
            "id": chg.id,
            "title": chg.title,
            "status": chg.status.value,
            "timeline_count": len(chg.timeline),
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_itil_cab_vote(args: dict) -> list[TextContent]:
    """Submit a CAB vote."""
    from ..itil import ITILManager

    change_id = args.get("change_id", "").strip()
    agent = args.get("agent", "").strip()
    if not change_id or not agent:
        return _error_response("change_id and agent are required")

    mgr = ITILManager(_shared_root())
    vote = mgr.submit_cab_vote(
        change_id=change_id,
        agent=agent,
        decision=args.get("decision", "abstain"),
        conditions=args.get("conditions", ""),
    )

    # Return current vote tally
    all_votes = mgr.get_cab_votes(change_id)
    tally = {
        "approved": sum(1 for v in all_votes if v.decision.value == "approved"),
        "rejected": sum(1 for v in all_votes if v.decision.value == "rejected"),
        "abstain": sum(1 for v in all_votes if v.decision.value == "abstain"),
    }

    return _json_response({
        "voted": True,
        "change_id": vote.change_id,
        "agent": vote.agent,
        "decision": vote.decision.value,
        "conditions": vote.conditions,
        "tally": tally,
    })


async def _handle_itil_status(_args: dict) -> list[TextContent]:
    """Return ITIL dashboard status."""
    from ..itil import ITILManager

    mgr = ITILManager(_shared_root())
    status = mgr.get_status()
    return _json_response(status)


async def _handle_itil_kedb_search(args: dict) -> list[TextContent]:
    """Search the Known Error Database."""
    from ..itil import ITILManager

    query = args.get("query", "").strip()
    if not query:
        return _error_response("query is required")

    mgr = ITILManager(_shared_root())
    results = mgr.search_kedb(query)
    return _json_response({
        "results": [
            {
                "id": e.id,
                "title": e.title,
                "symptoms": e.symptoms,
                "root_cause": e.root_cause,
                "workaround": e.workaround,
                "permanent_fix_change_id": e.permanent_fix_change_id,
                "related_problem_id": e.related_problem_id,
            }
            for e in results
        ],
        "total": len(results),
        "query": query,
    })


HANDLERS: dict = {
    "itil_incident_create": _handle_itil_incident_create,
    "itil_incident_update": _handle_itil_incident_update,
    "itil_incident_list": _handle_itil_incident_list,
    "itil_problem_create": _handle_itil_problem_create,
    "itil_problem_update": _handle_itil_problem_update,
    "itil_change_propose": _handle_itil_change_propose,
    "itil_change_update": _handle_itil_change_update,
    "itil_cab_vote": _handle_itil_cab_vote,
    "itil_status": _handle_itil_status,
    "itil_kedb_search": _handle_itil_kedb_search,
}
