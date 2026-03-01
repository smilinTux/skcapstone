"""Heartbeat beacon and peer discovery tools."""

from __future__ import annotations

from mcp.types import TextContent, Tool

from ._helpers import _get_agent_name, _home, _json_response

TOOLS: list[Tool] = [
    Tool(
        name="heartbeat_pulse",
        description=(
            "Publish a heartbeat beacon for this agent. "
            "Writes the agent's current state, capacity, and capabilities "
            "to the shared heartbeats directory so peers can discover it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "description": "Agent status: alive, busy, draining, offline (default: alive)",
                },
                "claimed_tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Currently claimed task IDs",
                },
                "loaded_model": {
                    "type": "string",
                    "description": "Currently loaded AI model name",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="heartbeat_peers",
        description=(
            "Discover all peers in the agent mesh from heartbeat files. "
            "Returns name, status, alive/stale, capabilities, and age "
            "for each peer. Stale heartbeats (past TTL) are marked offline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "include_self": {
                    "type": "boolean",
                    "description": "Include own heartbeat (default: false)",
                },
            },
            "required": [],
        },
    ),
    Tool(
        name="heartbeat_health",
        description=(
            "Get overall mesh health summary: total peers, alive/offline "
            "counts, aggregated capabilities across all live nodes."
        ),
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="heartbeat_find_capable",
        description=(
            "Find alive peers with a specific capability. "
            "Use this to locate agents that can perform a task."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "capability": {
                    "type": "string",
                    "description": "The capability name to search for",
                },
            },
            "required": ["capability"],
        },
    ),
]


async def _handle_heartbeat_pulse(args: dict) -> list[TextContent]:
    """Publish a heartbeat beacon."""
    from ..heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    hb = beacon.pulse(
        status=args.get("status", "alive"),
        claimed_tasks=args.get("claimed_tasks"),
        loaded_model=args.get("loaded_model", ""),
    )
    return _json_response({
        "agent_name": hb.agent_name,
        "status": hb.status,
        "hostname": hb.hostname,
        "platform": hb.platform,
        "ttl_seconds": hb.ttl_seconds,
        "uptime_hours": hb.uptime_hours,
        "capabilities": [c.name for c in hb.capabilities],
        "fingerprint": hb.fingerprint,
        "capacity": {
            "cpu_count": hb.capacity.cpu_count,
            "memory_total_mb": hb.capacity.memory_total_mb,
            "disk_free_gb": hb.capacity.disk_free_gb,
            "gpu_available": hb.capacity.gpu_available,
        },
    })


async def _handle_heartbeat_peers(args: dict) -> list[TextContent]:
    """Discover peers in the mesh."""
    from ..heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    peers = beacon.discover_peers(include_self=args.get("include_self", False))
    return _json_response([
        {
            "agent_name": p.agent_name,
            "status": p.status,
            "alive": p.alive,
            "age_seconds": p.age_seconds,
            "hostname": p.hostname,
            "capabilities": p.capabilities,
            "claimed_tasks": p.claimed_tasks,
        }
        for p in peers
    ])


async def _handle_heartbeat_health(_args: dict) -> list[TextContent]:
    """Get mesh health summary."""
    from ..heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    health = beacon.mesh_health()
    return _json_response({
        "total_peers": health.total_peers,
        "alive_peers": health.alive_peers,
        "offline_peers": health.offline_peers,
        "busy_peers": health.busy_peers,
        "total_capabilities": health.total_capabilities,
        "peers": [
            {"agent_name": p.agent_name, "status": p.status, "alive": p.alive}
            for p in health.peers
        ],
    })


async def _handle_heartbeat_find_capable(args: dict) -> list[TextContent]:
    """Find peers with a specific capability."""
    from ..heartbeat import HeartbeatBeacon

    home = _home()
    agent_name = _get_agent_name(home)
    beacon = HeartbeatBeacon(home, agent_name=agent_name)
    beacon.initialize()

    capability = args["capability"]
    peers = beacon.find_capable(capability)
    return _json_response({
        "capability": capability,
        "peers": [
            {"agent_name": p.agent_name, "status": p.status, "capabilities": p.capabilities}
            for p in peers
        ],
    })


HANDLERS: dict = {
    "heartbeat_pulse": _handle_heartbeat_pulse,
    "heartbeat_peers": _handle_heartbeat_peers,
    "heartbeat_health": _handle_heartbeat_health,
    "heartbeat_find_capable": _handle_heartbeat_find_capable,
}
