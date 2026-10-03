"""Trustee operations tools (health, restart, scale, rotate, monitor, logs, deployments).

trustee_restart, trustee_scale, and trustee_rotate are gated by
trustee_actuation.guard, enforced inside TrusteeOps itself (card e51a3e7e,
AUTONOMY_ARCHITECTURE.md section 3.5(d)): a refusal raises
trustee_actuation.ActuationRefusedError, caught here and turned into a
machine-readable `{"refused": true, "reason": ...}` response rather than an
uncaught exception. trustee_health, trustee_logs, trustee_deployments, and
trustee_monitor's own read/report surface are unaffected; trustee_monitor's
internal auto-restart/auto-rotate calls go through the same gated TrusteeOps
methods and already treat a raised refusal as "this attempt failed" (see
trustee_monitor.py's existing try/except around _ops.restart_agent /
_ops.rotate_agent), so no separate handling is needed for it here.
"""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ..trustee_actuation import ActuationRefusedError
from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="trustee_health",
        description=(
            "Run health checks on all agents in a deployment. Returns per-agent status, "
            "heartbeat, and error info."
        ),
        inputSchema={
            "properties": {
                "deployment_id": {"description": "The deployment ID to check", "type": "string"}
            },
            "required": ["deployment_id"],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_restart",
        description=(
            "Restart a failed agent or all agents in a deployment. Calls provider stop/start "
            "and updates deployment state."
        ),
        inputSchema={
            "properties": {
                "agent_name": {
                    "description": "Agent to restart (omit for all agents)",
                    "type": "string",
                },
                "deployment_id": {"description": "The deployment ID", "type": "string"},
            },
            "required": ["deployment_id"],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_scale",
        description=(
            "Scale the number of instances for an agent type up or down. Adds or removes "
            "instances while updating deployment state."
        ),
        inputSchema={
            "properties": {
                "agent_spec_key": {
                    "description": "The agent spec key (role) to scale",
                    "type": "string",
                },
                "count": {"description": "Desired total instance count (>= 1)", "type": "integer"},
                "deployment_id": {"description": "The deployment ID", "type": "string"},
            },
            "required": ["deployment_id", "agent_spec_key", "count"],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_rotate",
        description=(
            "Snapshot context, destroy, and redeploy an agent fresh. Used when an agent "
            "shows context degradation. Rotation is credential-adjacent and never routine: "
            "requires change_id to name a currently-APPROVED ITIL change, in addition to the "
            "actuation-readiness/freeze and capauth checks every trustee lifecycle verb "
            "requires. Refused (not raised as a bare error) with a machine-readable reason "
            "when any of those do not hold."
        ),
        inputSchema={
            "properties": {
                "agent_name": {"description": "Agent to rotate", "type": "string"},
                "deployment_id": {"description": "The deployment ID", "type": "string"},
                "change_id": {
                    "description": (
                        "ITIL change id authorizing this rotation. Must fold to APPROVED "
                        "status or the call is refused."
                    ),
                    "type": "string",
                },
            },
            "required": ["deployment_id", "agent_name"],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_monitor",
        description=(
            "Run a single autonomous monitoring pass over all deployments or a specific one. "
            "Detects stale heartbeats, triggers auto-restart/rotate, and escalates on "
            "critical degradation."
        ),
        inputSchema={
            "properties": {
                "auto_restart": {
                    "description": "Enable auto-restart on failure (default: true)",
                    "type": "boolean",
                },
                "auto_rotate": {
                    "description": "Enable auto-rotate after repeated failures (default: true)",
                    "type": "boolean",
                },
                "deployment_id": {
                    "description": "Specific deployment to check (omit for all)",
                    "type": "string",
                },
                "heartbeat_timeout": {
                    "description": "Seconds before heartbeat is stale (default: 120)",
                    "type": "number",
                },
            },
            "required": [],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_logs",
        description=(
            "Get recent log lines for agents in a deployment. Reads agent log files or falls "
            "back to audit log entries."
        ),
        inputSchema={
            "properties": {
                "agent_name": {"description": "Specific agent (omit for all)", "type": "string"},
                "deployment_id": {"description": "The deployment ID", "type": "string"},
                "tail": {"description": "Max lines per agent (default: 50)", "type": "integer"},
            },
            "required": ["deployment_id"],
            "type": "object",
        },
    ),
    Tool(
        name="trustee_deployments",
        description=(
            "List all active deployments with agent counts and status. Overview of the "
            "entire team fleet."
        ),
        inputSchema={"properties": {}, "required": [], "type": "object"},
    ),
]


# ── Helpers ──────────────────────────────────────────────────


def _refused_response(exc: ActuationRefusedError) -> list[TextContent]:
    """Turn a gate refusal into a machine-readable response.

    `reason` is one of trustee_actuation's REASON_* constants (`frozen`,
    `unprovisioned`, `capability_denied`, `change_not_approved`) so a caller
    can branch on it programmatically instead of parsing `message`.
    """
    return _json_response({"refused": True, "reason": exc.reason, "message": str(exc)})


def _get_trustee_ops():
    """Build TrusteeOps and TeamEngine from agent home."""
    from ..team_engine import TeamEngine
    from ..trustee_ops import TrusteeOps

    home = _home()
    engine = TeamEngine(home=home, provider=None, comms_root=None)
    ops = TrusteeOps(engine=engine, home=home)
    return ops, engine


# ── Handlers ─────────────────────────────────────────────────


async def _handle_trustee_health(args: dict) -> list[TextContent]:
    """Run health checks on a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    ops, _ = _get_trustee_ops()
    try:
        report = ops.health_report(deployment_id)
        healthy = sum(1 for r in report if r["healthy"])
        return _json_response(
            {
                "deployment_id": deployment_id,
                "agents": report,
                "summary": {
                    "total": len(report),
                    "healthy": healthy,
                    "degraded": len(report) - healthy,
                },
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_restart(args: dict) -> list[TextContent]:
    """Restart agents in a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    agent_name = args.get("agent_name")
    ops, _ = _get_trustee_ops()
    try:
        results = ops.restart_agent(deployment_id, agent_name)
        return _json_response(
            {
                "deployment_id": deployment_id,
                "results": results,
                "all_restarted": all(v == "restarted" for v in results.values()),
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))
    except ActuationRefusedError as exc:
        return _refused_response(exc)


async def _handle_trustee_scale(args: dict) -> list[TextContent]:
    """Scale agent instances in a deployment."""
    deployment_id = args.get("deployment_id", "")
    agent_spec_key = args.get("agent_spec_key", "")
    count = args.get("count", 0)
    if not deployment_id or not agent_spec_key or not count:
        return _error_response("deployment_id, agent_spec_key, and count are required")

    ops, _ = _get_trustee_ops()
    try:
        result = ops.scale_agent(deployment_id, agent_spec_key, count)
        return _json_response(
            {
                "deployment_id": deployment_id,
                "agent_spec_key": agent_spec_key,
                **result,
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))
    except ActuationRefusedError as exc:
        return _refused_response(exc)


async def _handle_trustee_rotate(args: dict) -> list[TextContent]:
    """Rotate an agent (snapshot + fresh deploy).

    Requires `change_id` to name a currently-APPROVED ITIL change (rotation
    is credential-adjacent and never routine); omitting it refuses exactly
    like naming an unapproved one, via ActuationRefusedError below.
    """
    deployment_id = args.get("deployment_id", "")
    agent_name = args.get("agent_name", "")
    change_id = args.get("change_id")
    if not deployment_id or not agent_name:
        return _error_response("deployment_id and agent_name are required")

    ops, _ = _get_trustee_ops()
    try:
        result = ops.rotate_agent(deployment_id, agent_name, change_id=change_id)
        return _json_response(
            {
                "deployment_id": deployment_id,
                "agent_name": agent_name,
                **result,
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))
    except ActuationRefusedError as exc:
        return _refused_response(exc)


async def _handle_trustee_monitor(args: dict) -> list[TextContent]:
    """Run a single monitoring pass."""
    from ..trustee_monitor import MonitorConfig, TrusteeMonitor

    ops, engine = _get_trustee_ops()
    config = MonitorConfig(
        heartbeat_timeout=args.get("heartbeat_timeout", 120.0),
        auto_restart=args.get("auto_restart", True),
        auto_rotate=args.get("auto_rotate", True),
    )
    monitor = TrusteeMonitor(ops, engine, config)

    deployment_id = args.get("deployment_id")
    if deployment_id:
        deployment = engine.get_deployment(deployment_id)
        if not deployment:
            return _error_response(f"Deployment '{deployment_id}' not found")
        report = monitor.check_deployment(deployment)
    else:
        report = monitor.check_all()

    return _json_response(
        {
            "timestamp": report.timestamp,
            "deployments_checked": report.deployments_checked,
            "agents_healthy": report.agents_healthy,
            "agents_degraded": report.agents_degraded,
            "restarts_triggered": report.restarts_triggered,
            "rotations_triggered": report.rotations_triggered,
            "escalations_sent": report.escalations_sent,
        }
    )


async def _handle_trustee_logs(args: dict) -> list[TextContent]:
    """Get agent logs from a deployment."""
    deployment_id = args.get("deployment_id", "")
    if not deployment_id:
        return _error_response("deployment_id is required")

    agent_name = args.get("agent_name")
    tail = args.get("tail", 50)
    ops, _ = _get_trustee_ops()
    try:
        logs = ops.get_logs(deployment_id, agent_name, tail=tail)
        return _json_response(
            {
                "deployment_id": deployment_id,
                "agents": {name: lines for name, lines in logs.items()},
            }
        )
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_deployments(_args: dict) -> list[TextContent]:
    """List all active deployments."""
    _, engine = _get_trustee_ops()
    deployments = engine.list_deployments()
    return _json_response(
        {
            "count": len(deployments),
            "deployments": [
                {
                    "deployment_id": d.deployment_id,
                    "blueprint_slug": d.blueprint_slug,
                    "team_name": d.team_name,
                    "provider": d.provider,
                    "status": d.status,
                    "agent_count": len(d.agents),
                    "agents": {
                        name: {
                            "status": (
                                a.status.value if hasattr(a.status, "value") else str(a.status)
                            ),
                            "host": a.host or "-",
                            "last_heartbeat": a.last_heartbeat or "-",
                        }
                        for name, a in d.agents.items()
                    },
                }
                for d in deployments
            ],
        }
    )


HANDLERS: dict = {
    "trustee_health": _handle_trustee_health,
    "trustee_restart": _handle_trustee_restart,
    "trustee_scale": _handle_trustee_scale,
    "trustee_rotate": _handle_trustee_rotate,
    "trustee_monitor": _handle_trustee_monitor,
    "trustee_logs": _handle_trustee_logs,
    "trustee_deployments": _handle_trustee_deployments,
}
