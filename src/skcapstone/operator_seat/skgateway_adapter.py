"""skgateway operator adapter: Atlas manages the model gateway too (O7 app adapter).

Conformant to the adapter contract (explain / observe / act). The health probe is
injectable so tests never touch a live skgateway; the default reads the gateway
health endpoint and fails safe (reports healthy) rather than raising a false alarm
when it cannot be reached.
"""

from __future__ import annotations

from typing import Callable

CONDITIONS = ["UpstreamServing", "PoolHealthy"]

#: Health-type conditions (fire when status is False): a dead upstream or a
#: saturated connection pool (the 172-of-48 case) both read as False -> firing.
_ACTIONS = [
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the skgateway service",
        "kedb_refs": [],
    },
    {
        "name": "quarantine_dead_alias",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "drop a degraded upstream from the pool, auto-restore on recovery",
        "kedb_refs": [],
    },
    {
        "name": "raise_pool_limit",
        "standard": False,
        "reversible": True,
        "blast_radius": "medium",
        "runbook": "raise the NIM connection-pool ceiling (major: escalates)",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


def _default_probe() -> dict:
    """Best-effort skgateway health. Fails SAFE (healthy) when unreachable."""
    try:
        import json
        import os
        import urllib.request

        base = os.environ.get("SKOPERATOR_GATEWAY", "http://localhost:18780/v1")
        url = base.rstrip("/").rsplit("/v1", 1)[0] + "/health"
        with urllib.request.urlopen(url, timeout=8) as r:
            h = json.loads(r.read())
        backends = h.get("backends", {})
        up = all(b.get("status") == "up" for b in backends.values()) if backends else True
        saturated = any(b.get("quarantined") for b in backends.values())
        return {"upstream_serving": bool(up), "pool_healthy": not saturated}
    except Exception as exc:
        return {"_probe_error": type(exc).__name__}


def skgateway_explain() -> dict:
    """skgateway's self-description in the adapter-contract shape."""
    return {
        "kinds": ["upstream", "pool"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skgateway_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skgateway health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    unknown = bool(st.get("_probe_error"))
    return {
        "conditions": [
            {
                "type": "UpstreamServing",
                "status": "Unknown" if unknown else _b(bool(st.get("upstream_serving"))),
                "object": "upstreams",
            },
            {
                "type": "PoolHealthy",
                "status": "Unknown" if unknown else _b(bool(st.get("pool_healthy"))),
                "object": "connection-pool",
            },
        ]
    }


#: A loop-compatible observe (paths, now_iso) -> {conditions}; ignores both.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skgateway_observe()


__all__ = ["skgateway_explain", "skgateway_observe", "observe", "CONDITIONS"]
