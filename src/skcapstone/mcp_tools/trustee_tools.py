"""Trustee operations tools (health, restart, scale, rotate, monitor, logs, deployments)."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _error_response, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="trustee_health",
        description=(
            "Run health checks on all agents in a deployment. "
            "Returns per-agent status, heartbeat, and error info."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "The deployment ID to check",
                },
            },
            "required": ["deployment_id"],
        },
    ),
    Tool(
        name="trustee_restart",
        description=(
            "Restart a failed agent or all agents in a deployment. "
            "Calls provider stop/start and updates deployment state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "The deployment ID",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent to restart (omit for all agents)",
                },
            },
            "required": ["deployment_id"],
        },
    ),
    Tool(
        name="trustee_scale",
        description=(
            "Scale the number of instances for an agent type up or down. "
            "Adds or removes instances while updating deployment state."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "The deployment ID",
                },
                "agent_spec_key": {
                    "type": "string",
                    "description": "The agent spec key (role) to scale",
                },
                "count": {
                    "type": "integer",
                    "description": "Desired total instance count (>= 1)",
                },
            },
            "required": ["deployment_id", "agent_spec_key", "count"],
        },
    ),
    Tool(
        name="trustee_rotate",
        description=(
            "Snapshot context, destroy, and redeploy an agent fresh. "
            "Used when an agent shows context degradation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "The deployment ID",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Agent to rotate",
                },
            },
            "required": ["deployment_id", "agent_name"],
        },
    ),
    Tool(
        name="trustee_monitor",
        description=(
            "Run a single autonomous monitoring pass over all deployments "
            "or a specific one. Detects stale heartbeats, triggers "
            "auto-restart/rotate, and escalates on critical degradation."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "Specific deployment to check (omit for all)",
                },
                "heartbeat_timeout": {
                    "type": "number",
                    "description": "Seconds before heartbeat is stale (default: 120)",
                },
                "auto_restart": {
                    "type": "boolean",
                    "description": "Enable auto-restart on failure (default: true)",
                },
                "auto_rotate": {
                    "type": "boolean",
                    "description": "Enable auto-rotate after repeated failures (default: true)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="trustee_logs",
        description=(
            "Get recent log lines for agents in a deployment. "
            "Reads agent log files or falls back to audit log entries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "deployment_id": {
                    "type": "string",
                    "description": "The deployment ID",
                },
                "agent_name": {
                    "type": "string",
                    "description": "Specific agent (omit for all)",
                },
                "tail": {
                    "type": "integer",
                    "description": "Max lines per agent (default: 50)",
                },
            },
            "required": ["deployment_id"],
        },
    ),
    Tool(
        name="trustee_deployments",
        description=(
            "List all active deployments with agent counts and status. "
            "Overview of the entire team fleet."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
]


# ── Helpers ──────────────────────────────────────────────────


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
        return _json_response({
            "deployment_id": deployment_id,
            "agents": report,
            "summary": {
                "total": len(report),
                "healthy": healthy,
                "degraded": len(report) - healthy,
            },
        })
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
        return _json_response({
            "deployment_id": deployment_id,
            "results": results,
            "all_restarted": all(v == "restarted" for v in results.values()),
        })
    except ValueError as exc:
        return _error_response(str(exc))


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
        return _json_response({
            "deployment_id": deployment_id,
            "agent_spec_key": agent_spec_key,
            **result,
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_rotate(args: dict) -> list[TextContent]:
    """Rotate an agent (snapshot + fresh deploy)."""
    deployment_id = args.get("deployment_id", "")
    agent_name = args.get("agent_name", "")
    if not deployment_id or not agent_name:
        return _error_response("deployment_id and agent_name are required")

    ops, _ = _get_trustee_ops()
    try:
        result = ops.rotate_agent(deployment_id, agent_name)
        return _json_response({
            "deployment_id": deployment_id,
            "agent_name": agent_name,
            **result,
        })
    except ValueError as exc:
        return _error_response(str(exc))


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

    return _json_response({
        "timestamp": report.timestamp,
        "deployments_checked": report.deployments_checked,
        "agents_healthy": report.agents_healthy,
        "agents_degraded": report.agents_degraded,
        "restarts_triggered": report.restarts_triggered,
        "rotations_triggered": report.rotations_triggered,
        "escalations_sent": report.escalations_sent,
    })


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
        return _json_response({
            "deployment_id": deployment_id,
            "agents": {name: lines for name, lines in logs.items()},
        })
    except ValueError as exc:
        return _error_response(str(exc))


async def _handle_trustee_deployments(_args: dict) -> list[TextContent]:
    """List all active deployments."""
    _, engine = _get_trustee_ops()
    deployments = engine.list_deployments()
    return _json_response({
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
                        "status": a.status.value if hasattr(a.status, "value") else str(a.status),
                        "host": a.host or "\u2014",
                        "last_heartbeat": a.last_heartbeat or "\u2014",
                    }
                    for name, a in d.agents.items()
                },
            }
            for d in deployments
        ],
    })


HANDLERS: dict = {
    "trustee_health": _handle_trustee_health,
    "trustee_restart": _handle_trustee_restart,
    "trustee_scale": _handle_trustee_scale,
    "trustee_rotate": _handle_trustee_rotate,
    "trustee_monitor": _handle_trustee_monitor,
    "trustee_logs": _handle_trustee_logs,
    "trustee_deployments": _handle_trustee_deployments,
}
