"""ITIL service management tools - Incident, Problem, Change, KEDB."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _json_response, _shared_root

# Default grace window for an ASAP schedule (CM P1.2 / design doc section 4.3:
# "ASAP is not a special case: it is window_start = now, window_end = now + a
# default grace (e.g. 4h), asap: true"). Also used as the grace after an
# explicit `--at` window start, so every schedule carries a real window_end
# for the (later) deploy runner's "window arrived" check.
_SCHEDULE_GRACE_HOURS = 4

# ═══════════════════════════════════════════════════════════
# Tool Definitions
# ═══════════════════════════════════════════════════════════

TOOLS: list[Tool] = [
    Tool(
        name="itil_incident_create",
        description=(
            "Create a new ITIL incident for a service disruption. Auto-creates a linked GTD "
            "item (next-action for sev1/sev2, inbox for sev3/sev4)."
        ),
        inputSchema={
            "properties": {
                "affected_services": {
                    "description": "List of affected service names",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "impact": {"description": "Business impact description", "type": "string"},
                "managed_by": {
                    "description": "Agent responsible for managing this incident",
                    "type": "string",
                },
                "severity": {
                    "description": "Severity level (default: sev3)",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "type": "string",
                },
                "source": {
                    "description": "Detection source (default: manual)",
                    "enum": ["service_health", "dreaming", "manual", "daemon_error", "heartbeat"],
                    "type": "string",
                },
                "tags": {
                    "description": "Tags for categorization",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "title": {"description": "Brief description of the incident", "type": "string"},
            },
            "required": ["title"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_incident_update",
        description=(
            "Update an incident: transition status, escalate severity, add timeline notes, "
            "or resolve. Valid status transitions (per _INCIDENT_TRANSITIONS): detected -> "
            "{acknowledged, escalated, resolved}; acknowledged -> {investigating, escalated, "
            "resolved}; investigating -> {escalated, resolved}; escalated -> {investigating, "
            "resolved}; resolved -> {closed}; closed is terminal. A separate reopen event "
            "moves resolved -> investigating (fold-only, clears "
            "resolved_at/resolution_summary)."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Agent making the update", "type": "string"},
                "incident_id": {
                    "description": "Incident ID (e.g. inc-a1b2c3d4)",
                    "type": "string",
                },
                "new_status": {
                    "description": "New status",
                    "enum": ["acknowledged", "investigating", "escalated", "resolved", "closed"],
                    "type": "string",
                },
                "note": {"description": "Timeline note", "type": "string"},
                "managed_by": {
                    "description": "Assign incident management to an agent",
                    "type": "string",
                },
                "related_problem_id": {
                    "description": "Link to a related problem record",
                    "type": "string",
                },
                "resolution_summary": {
                    "description": "Resolution summary (when resolving)",
                    "type": "string",
                },
                "severity": {
                    "description": "New severity",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "type": "string",
                },
            },
            "required": ["incident_id", "agent"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_incident_list",
        description="List ITIL incidents filtered by status, severity, or affected service.",
        inputSchema={
            "properties": {
                "service": {"description": "Filter by affected service name", "type": "string"},
                "severity": {
                    "description": "Filter by severity",
                    "enum": ["sev1", "sev2", "sev3", "sev4"],
                    "type": "string",
                },
                "status": {
                    "description": "Filter by status",
                    "enum": [
                        "detected",
                        "acknowledged",
                        "investigating",
                        "escalated",
                        "resolved",
                        "closed",
                    ],
                    "type": "string",
                },
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="itil_problem_create",
        description=(
            "Create a new ITIL problem record to investigate root cause. Links to related "
            "incidents and auto-creates a GTD project."
        ),
        inputSchema={
            "properties": {
                "managed_by": {
                    "description": "Agent responsible for investigation",
                    "type": "string",
                },
                "related_incident_ids": {
                    "description": "Related incident IDs",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "tags": {
                    "description": "Tags for categorization",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "title": {"description": "Problem title", "type": "string"},
                "workaround": {"description": "Known workaround if any", "type": "string"},
            },
            "required": ["title"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_problem_update",
        description=(
            "Update a problem record: transition status, set root cause, add workaround, "
            "optionally create a KEDB entry. Valid transitions: "
            "identified->analyzing->known_error->resolved."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Agent making the update", "type": "string"},
                "create_kedb": {
                    "description": "Create a KEDB entry from this problem",
                    "type": "boolean",
                },
                "new_status": {
                    "description": "New status",
                    "enum": ["analyzing", "known_error", "resolved"],
                    "type": "string",
                },
                "note": {"description": "Timeline note", "type": "string"},
                "problem_id": {"description": "Problem ID (e.g. prb-e5f6g7h8)", "type": "string"},
                "root_cause": {"description": "Root cause description", "type": "string"},
                "workaround": {"description": "Workaround description", "type": "string"},
            },
            "required": ["problem_id", "agent"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_change_propose",
        description=(
            "Propose a change (RFC). Standard changes auto-approve at fold time. "
            "Operator-authored normal changes tagged 'auto-normal' (not high-risk, with a "
            "rollback plan, no rejection) also auto-approve. All other normal changes and "
            "every emergency change follow the CAB path: a human approval unblocks, any "
            "rejection blocks. Emergency changes have no timeout or fast-path auto-approval."
        ),
        inputSchema={
            "properties": {
                "change_type": {
                    "description": "Type of change (default: normal)",
                    "enum": ["standard", "normal", "emergency"],
                    "type": "string",
                },
                "implementer": {
                    "description": "Agent who will implement the change",
                    "type": "string",
                },
                "managed_by": {"description": "Agent managing the change", "type": "string"},
                "related_problem_id": {
                    "description": "Related problem ID if applicable",
                    "type": "string",
                },
                "risk": {
                    "description": "Risk level (default: medium)",
                    "enum": ["low", "medium", "high"],
                    "type": "string",
                },
                "rollback_plan": {
                    "description": "How to roll back if the change fails",
                    "type": "string",
                },
                "tags": {
                    "description": "Tags for categorization",
                    "items": {"type": "string"},
                    "type": "array",
                },
                "test_plan": {"description": "How to verify the change works", "type": "string"},
                "title": {"description": "Change title", "type": "string"},
            },
            "required": ["title"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_change_update",
        description=(
            "Update a change: transition status (implementing, deployed, verified, failed, "
            "closed) or add timeline notes."
        ),
        inputSchema={
            "properties": {
                "agent": {"description": "Agent making the update", "type": "string"},
                "change_id": {"description": "Change ID (e.g. chg-i1j2k3l4)", "type": "string"},
                "new_status": {
                    "description": "New status",
                    "enum": [
                        "reviewing",
                        "approved",
                        "rejected",
                        "implementing",
                        "deployed",
                        "verified",
                        "failed",
                        "closed",
                    ],
                    "type": "string",
                },
                "note": {"description": "Timeline note", "type": "string"},
            },
            "required": ["change_id", "agent"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_change_validate",
        description=(
            "Attach a CI validation verdict to a change's draft PR. Appends a `validation` "
            "event (event-sourced, fold-derived, latest wins). A passing verdict while the "
            "change is still 'proposed' auto-advances it to 'reviewing' (ready for CAB); a "
            "failing verdict leaves status unchanged. Advisory input to CAB, never a "
            "substitute for it - the card popout shows the verdict chip next to the vote "
            "tally. NOTE: capability `change.validate` (attested tier) is the intended PDP "
            "gate for this transition (design doc section 7); this MCP layer does not yet "
            "call capauth.decide() itself - no existing itil_* tool does - so enforcement "
            "for now lives at the (later) dashboard route card."
        ),
        inputSchema={
            "properties": {
                "change_id": {"description": "Change ID (e.g. chg-i1j2k3l4)", "type": "string"},
                "agent": {
                    "description": "Agent/system attaching the verdict (default: ci)",
                    "type": "string",
                },
                "passed": {"description": "Whether the checks passed", "type": "boolean"},
                "head_sha": {
                    "description": (
                        "Git SHA the checks ran against - the deploy executor later refuses "
                        "a stale verdict whose head_sha does not match the PR's current HEAD"
                    ),
                    "type": "string",
                },
                "url": {"description": "URL to the CI run / PR checks", "type": "string"},
                "summary": {"description": "Free-text summary of the verdict", "type": "string"},
                "checks": {
                    "description": "Per-check breakdown (name/status pairs)",
                    "items": {"type": "object"},
                    "type": "array",
                },
            },
            "required": ["change_id", "passed"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_change_schedule",
        description=(
            "Schedule an APPROVED change for deployment: ASAP (now + a "
            f"{_SCHEDULE_GRACE_HOURS}h grace window) or a specific window start (`at`, ISO "
            "8601), plus a deploy_mode. Appends a `schedule` event; valid ONLY while the "
            "change is 'approved' (fold-enforced, same fail-closed treatment as an invalid "
            "status transition) - scheduling a change that is not approved is refused and "
            "returned as `scheduled: false` with no state change. Re-schedule is unschedule "
            "+ schedule again. NOTE: capability `change.schedule` (verified tier) is the "
            "intended PDP gate (design doc section 7); not yet enforced at this MCP layer - "
            "see itil_change_validate's note."
        ),
        inputSchema={
            "properties": {
                "change_id": {"description": "Change ID (e.g. chg-i1j2k3l4)", "type": "string"},
                "agent": {
                    "description": "Agent/operator scheduling the change (default: human)",
                    "type": "string",
                },
                "asap": {
                    "description": (
                        f"Schedule ASAP (now + {_SCHEDULE_GRACE_HOURS}h grace window). "
                        "Mutually exclusive with `at`."
                    ),
                    "type": "boolean",
                },
                "at": {
                    "description": "ISO 8601 window start. Mutually exclusive with `asap`.",
                    "type": "string",
                },
                "deploy_mode": {
                    "description": "Deploy mode (default: confirm - requires a human arm)",
                    "enum": ["confirm", "auto"],
                    "type": "string",
                },
                "note": {"description": "Timeline note", "type": "string"},
            },
            "required": ["change_id"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_change_unschedule",
        description=(
            "Unschedule a change: scheduled -> approved, clears the scheduled window. Valid "
            "only while the change is 'scheduled' (fold-enforced); a no-op (returned as "
            "`unscheduled: false`) on any other status. NOTE: capability `change.schedule` "
            "(verified tier) is the intended PDP gate; not yet enforced at this MCP layer - "
            "see itil_change_validate's note."
        ),
        inputSchema={
            "properties": {
                "change_id": {"description": "Change ID (e.g. chg-i1j2k3l4)", "type": "string"},
                "agent": {
                    "description": "Agent/operator unscheduling the change (default: human)",
                    "type": "string",
                },
                "note": {"description": "Timeline note", "type": "string"},
            },
            "required": ["change_id"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_cab_vote",
        description=(
            "Submit a CAB (Change Advisory Board) vote for a proposed change. Each agent "
            "writes its own vote file (conflict-free). A human rejection blocks the change; "
            "a human approval unblocks it. CR change-mgmt P1.4: the voter of record is the "
            "caller's capauth-resolved authenticated identity when one can be resolved, NOT "
            "the free-text `agent` argument - this closes the anonymous-voting hole where "
            "any caller could write agent='human' and unblock a change. `agent` is kept for "
            "back-compat display / legacy callers and is used as the voter only when no "
            "authenticated identity is resolvable. NOTE: capability `change.cab_vote` "
            "(verified tier) is the intended PDP gate (design doc section 7); not yet "
            "enforced at this MCP layer - see itil_change_validate's note."
        ),
        inputSchema={
            "properties": {
                "agent": {
                    "description": (
                        "Free-text voter label. Used as the voter identity only when the "
                        "caller's authenticated identity cannot be resolved."
                    ),
                    "type": "string",
                },
                "change_id": {"description": "Change ID to vote on", "type": "string"},
                "conditions": {"description": "Conditions for approval", "type": "string"},
                "decision": {
                    "description": "Vote decision (default: abstain)",
                    "enum": ["approved", "rejected", "abstain"],
                    "type": "string",
                },
            },
            "required": ["change_id", "agent"],
            "type": "object",
        },
    ),
    Tool(
        name="itil_status",
        description=(
            "ITIL dashboard: open incidents by severity, active problems, pending changes, "
            "and KEDB count."
        ),
        inputSchema={"properties": {}, "required": [], "type": "object"},
    ),
    Tool(
        name="itil_kedb_search",
        description=(
            "Search the Known Error Database by symptoms, service name, or keywords. Returns "
            "matching entries with workarounds."
        ),
        inputSchema={
            "properties": {
                "query": {
                    "description": "Search query (matches title, symptoms, root cause, tags)",
                    "type": "string",
                }
            },
            "required": ["query"],
            "type": "object",
        },
    ),
]


# ═══════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════


def _resolve_authenticated_subject() -> str | None:
    """Resolve the calling process's capauth-authenticated identity.

    CR change-mgmt P1.4 (the anonymous-voting fix): the CAB voter of record
    must be a real authenticated identity, not a free-text argument the
    caller supplies. Delegates to the one canonical resolver
    (``capauth.resolve_agent_identity``) instead of reimplementing identity
    logic, mirroring the same try/except-and-fall-back-to-None shape already
    used by ``skcapstone.fleet.store.writer_identity`` and
    ``cli/identity_cmd.py``. Returns the short agent name (``.agent``, e.g.
    ``"lumina"``) - the identity shape every existing CAB vote / `prepared_by`
    / `writer` field in skcoord.itil already uses - never raises, so a
    dev/legacy environment without capauth installed falls back to
    ``submit_cab_vote``'s pre-existing free-text behavior (``subject=None``).
    """
    try:
        from capauth import resolve_agent_identity

        ident = resolve_agent_identity()
        return ident.agent or None
    except Exception:  # noqa: BLE001 - resolver failure must never crash a vote
        return None


def _resolve_change_or_raise(mgr, change_id: str) -> str:
    """Resolve a change id (following redirect stubs) and raise if unknown.

    Mirrors the existence check ``update_change`` already does internally;
    the new validate/schedule/unschedule transitions have no dedicated
    public ``ITILManager`` method (they append events directly, exactly as
    skcoord's own ``test_change_management.py`` does), so this helper keeps
    that same "raise ValueError for an unknown change" contract instead of
    silently creating an orphan events directory for a bad id.
    """
    rid = mgr._resolve_id(mgr.changes_dir, change_id)
    if mgr._load_core(mgr.changes_dir, rid) is None:
        raise ValueError(f"Change {change_id} not found")
    return rid


def _schedule_window(asap: bool, at: str | None) -> tuple[str, str]:
    """Compute (window_start, window_end) for a schedule event.

    Per the design doc (section 4.3): "ASAP is not a special case: it is
    window_start = now, window_end = now + a default grace (e.g. 4h), asap:
    true." The same grace is applied after an explicit `at` start so every
    schedule carries a real window_end for the (later) deploy runner's
    "window arrived" check.
    """
    if at:
        start = datetime.fromisoformat(at.replace("Z", "+00:00"))
    else:
        start = datetime.now(timezone.utc)
    end = start + timedelta(hours=_SCHEDULE_GRACE_HOURS)
    return start.isoformat(), end.isoformat()


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
    return _json_response(
        {
            "created": True,
            "id": incident.id,
            "title": incident.title,
            "severity": incident.severity.value,
            "status": incident.status.value,
            "managed_by": incident.managed_by,
            "gtd_item_ids": incident.gtd_item_ids,
        }
    )


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
            managed_by=args.get("managed_by"),
        )
        return _json_response(
            {
                "updated": True,
                "id": inc.id,
                "title": inc.title,
                "severity": inc.severity.value,
                "status": inc.status.value,
                "timeline_count": len(inc.timeline),
            }
        )
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
    return _json_response(
        {
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
        }
    )


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
    return _json_response(
        {
            "created": True,
            "id": problem.id,
            "title": problem.title,
            "status": problem.status.value,
            "managed_by": problem.managed_by,
            "related_incident_ids": problem.related_incident_ids,
        }
    )


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
        return _json_response(
            {
                "updated": True,
                "id": prb.id,
                "title": prb.title,
                "status": prb.status.value,
                "root_cause": prb.root_cause,
                "kedb_id": prb.kedb_id,
                "timeline_count": len(prb.timeline),
            }
        )
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
    return _json_response(
        {
            "created": True,
            "id": change.id,
            "title": change.title,
            "change_type": change.change_type.value,
            "status": change.status.value,
            "cab_required": change.cab_required,
            "managed_by": change.managed_by,
        }
    )


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
        return _json_response(
            {
                "updated": True,
                "id": chg.id,
                "title": chg.title,
                "status": chg.status.value,
                "timeline_count": len(chg.timeline),
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_itil_change_validate(args: dict) -> list[TextContent]:
    """Attach a CI validation verdict to a change's draft PR."""
    from ..itil import Change, ITILManager

    change_id = args.get("change_id", "").strip()
    if not change_id:
        return _error_response("change_id is required")
    if args.get("passed") is None:
        return _error_response("passed is required")
    agent = args.get("agent", "").strip() or "ci"

    mgr = ITILManager(_shared_root())
    try:
        rid = _resolve_change_or_raise(mgr, change_id)
    except ValueError as exc:
        return _error_response(str(exc))

    mgr._append_event(
        mgr.changes_dir,
        rid,
        agent,
        "validation",
        passed=bool(args.get("passed")),
        head_sha=args.get("head_sha"),
        url=args.get("url"),
        summary=args.get("summary"),
        checks=args.get("checks"),
    )
    chg = mgr._fold_record(mgr.changes_dir, rid, Change)
    return _json_response(
        {
            "validated": True,
            "id": chg.id,
            "status": chg.status.value,
            "validation": chg.validation,
        }
    )


async def _handle_itil_change_schedule(args: dict) -> list[TextContent]:
    """Schedule an approved change for deployment (ASAP or a window start)."""
    from ..itil import Change, ITILManager

    change_id = args.get("change_id", "").strip()
    if not change_id:
        return _error_response("change_id is required")
    asap = bool(args.get("asap", False))
    at = args.get("at")
    if asap and at:
        return _error_response("asap and at are mutually exclusive")
    agent = args.get("agent", "").strip() or "human"

    mgr = ITILManager(_shared_root())
    try:
        rid = _resolve_change_or_raise(mgr, change_id)
    except ValueError as exc:
        return _error_response(str(exc))

    window_start, window_end = _schedule_window(asap, at)
    mgr._append_event(
        mgr.changes_dir,
        rid,
        agent,
        "schedule",
        window_start=window_start,
        window_end=window_end,
        asap=asap,
        deploy_mode=args.get("deploy_mode") or "confirm",
        note=args.get("note", ""),
    )
    chg = mgr._fold_record(mgr.changes_dir, rid, Change)
    if chg.status.value != "scheduled":
        # Fold refused the transition (fail-closed): the change was not
        # 'approved' at the time the event was appended.
        return _json_response(
            {
                "scheduled": False,
                "id": chg.id,
                "status": chg.status.value,
                "reason": (
                    "schedule is only valid while the change is 'approved' "
                    "(fold refused the transition)"
                ),
            }
        )
    return _json_response(
        {
            "scheduled": True,
            "id": chg.id,
            "status": chg.status.value,
            "scheduled_window": chg.scheduled_window,
        }
    )


async def _handle_itil_change_unschedule(args: dict) -> list[TextContent]:
    """Unschedule a change: scheduled -> approved, clears the window."""
    from ..itil import Change, ITILManager

    change_id = args.get("change_id", "").strip()
    if not change_id:
        return _error_response("change_id is required")
    agent = args.get("agent", "").strip() or "human"

    mgr = ITILManager(_shared_root())
    try:
        rid = _resolve_change_or_raise(mgr, change_id)
    except ValueError as exc:
        return _error_response(str(exc))

    was_scheduled = mgr._fold_record(mgr.changes_dir, rid, Change).status.value == "scheduled"
    mgr._append_event(mgr.changes_dir, rid, agent, "unschedule", note=args.get("note", ""))
    chg = mgr._fold_record(mgr.changes_dir, rid, Change)
    return _json_response(
        {
            "unscheduled": was_scheduled,
            "id": chg.id,
            "status": chg.status.value,
        }
    )


async def _handle_itil_cab_vote(args: dict) -> list[TextContent]:
    """Submit a CAB vote."""
    from ..itil import ITILManager

    change_id = args.get("change_id", "").strip()
    agent = args.get("agent", "").strip()
    if not change_id or not agent:
        return _error_response("change_id and agent are required")

    # CR change-mgmt P1.4: bind the vote to the caller's authenticated
    # identity, never the free-text `agent` arg above - `agent` only becomes
    # the voter of record when no authenticated subject is resolvable
    # (submit_cab_vote's own back-compat contract for `subject=None`).
    subject = _resolve_authenticated_subject()

    mgr = ITILManager(_shared_root())
    vote = mgr.submit_cab_vote(
        change_id=change_id,
        agent=agent,
        decision=args.get("decision", "abstain"),
        conditions=args.get("conditions", ""),
        subject=subject,
    )

    # Return current vote tally
    all_votes = mgr.get_cab_votes(change_id)
    tally = {
        "approved": sum(1 for v in all_votes if v.decision.value == "approved"),
        "rejected": sum(1 for v in all_votes if v.decision.value == "rejected"),
        "abstain": sum(1 for v in all_votes if v.decision.value == "abstain"),
    }

    return _json_response(
        {
            "voted": True,
            "change_id": vote.change_id,
            "agent": vote.agent,
            "decision": vote.decision.value,
            "conditions": vote.conditions,
            "tally": tally,
        }
    )


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
    return _json_response(
        {
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
        }
    )


HANDLERS: dict = {
    "itil_incident_create": _handle_itil_incident_create,
    "itil_incident_update": _handle_itil_incident_update,
    "itil_incident_list": _handle_itil_incident_list,
    "itil_problem_create": _handle_itil_problem_create,
    "itil_problem_update": _handle_itil_problem_update,
    "itil_change_propose": _handle_itil_change_propose,
    "itil_change_update": _handle_itil_change_update,
    "itil_change_validate": _handle_itil_change_validate,
    "itil_change_schedule": _handle_itil_change_schedule,
    "itil_change_unschedule": _handle_itil_change_unschedule,
    "itil_cab_vote": _handle_itil_cab_vote,
    "itil_status": _handle_itil_status,
    "itil_kedb_search": _handle_itil_kedb_search,
}
