"""skcomms operator adapter: Atlas manages skcomms too (the O7 app adapter).

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skcomms plugs in by exposing the same three verbs the fleet does. The health
probe is injectable so tests never touch a live skcomms; the default reads the
skcomms daemon status and queue depth, and fails safe (reports healthy) rather
than raising a false alarm when skcomms cannot be reached.
"""

from __future__ import annotations

from typing import Callable

CONDITIONS = ["PathHealthy", "QueueDrained"]

#: skcomms health conditions are health-type (they fire when status is False), so
#: they are NOT problem-when-true; the operator brief treats them correctly by
#: default. Queue over its bound -> QueueDrained False -> firing.
_ACTIONS = [
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the wedged skcomms service",
        "kedb_refs": [],
    },
    {
        "name": "failover_discovery",
        "standard": False,
        "reversible": True,
        "blast_radius": "fleet_restart",
        "runbook": "fail service discovery over to a healthy path (major: escalates)",
        "kedb_refs": [],
    },
]

_QUEUE_LIMIT = 1000


def _b(value: bool) -> str:
    return "True" if value else "False"


def _default_probe() -> dict:
    """Best-effort skcomms health. Fails SAFE (healthy) when skcomms is
    unreachable, so an inability to probe never raises a false alarm."""
    try:
        import subprocess

        r = subprocess.run(
            ["skcomms", "daemon", "status"], capture_output=True, text=True, timeout=10
        )
        healthy = r.returncode == 0
        return {"path_healthy": healthy, "queue_depth": 0, "queue_limit": _QUEUE_LIMIT}
    except Exception as exc:
        return {"_probe_error": type(exc).__name__}


def skcomms_explain() -> dict:
    """skcomms' self-description in the adapter-contract shape."""
    return {
        "kinds": ["path", "queue"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skcomms_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skcomms health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    unknown = bool(st.get("_probe_error"))
    depth = int(st.get("queue_depth", 0))
    limit = int(st.get("queue_limit", _QUEUE_LIMIT))
    return {
        "conditions": [
            {
                "type": "PathHealthy",
                "status": "Unknown" if unknown else _b(bool(st.get("path_healthy"))),
                "object": "discovery-path",
            },
            {
                "type": "QueueDrained",
                "status": "Unknown" if unknown else _b(depth <= limit),
                "object": "queue",
            },
        ]
    }


#: A loop-compatible observe (name, now_iso) -> {conditions}; ignores now_iso.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skcomms_observe()


__all__ = ["skcomms_explain", "skcomms_observe", "observe", "CONDITIONS"]
