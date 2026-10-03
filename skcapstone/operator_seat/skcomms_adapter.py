"""skcomms operator adapter: Atlas manages skcomms too (the O7 app adapter).

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skcomms plugs in by exposing the same three verbs the fleet does. The health
probe is injectable so tests never touch a live skcomms.

The default probe DELEGATES to skcomms' own operator-facet contract
(``skcomms.operator_probe``, the exact module ``skcomms operator observe`` runs)
instead of maintaining a second, independent signal reader here. This is a fix
for card 504d0046 (ATLAS Eyes PR #178 first run): the old default probe shelled
out to ``skcomms daemon status``, a subcommand that no longer exists (the CLI
answers exit 2, "no such command"), which read as a confidently WRONG
``PathHealthy=False``; and it hardcoded ``queue_depth: 0``, which read as a
confidently WRONG ``QueueDrained=True`` no matter how deep the real backlog was
(the exact class of blind spot that let a 140k-file outbox leak go unseen).
Delegating to the real, tested probe (``queue_depth()``, already the single
canonical backlog metric per coord eb659f61 / roadmap CR-5.3, plus
``operator_probe.observe()`` for ``PathHealthy``) makes this ONE real signal with
two callers (in-process seat, out-of-process cli), so the two lanes cannot drift
again short of the underlying probe itself changing behavior mid-flight. Fails
SAFE (healthy) when skcomms is not importable or the probe raises, so an
inability to probe never raises a false alarm.
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
    """Best-effort skcomms health, delegated to ``skcomms.operator_probe`` (the
    same real-signal module the ``skcomms operator observe`` cli lane runs).
    Fails SAFE (healthy) when skcomms is not importable or the probe raises, so
    an inability to probe never raises a false alarm."""
    try:
        from skcomms.operator_probe import observe as _skcomms_operator_observe
        from skcomms.operator_probe import queue_depth

        by_type = {c["type"]: c["status"] for c in _skcomms_operator_observe()["conditions"]}
        return {
            "path_healthy": by_type.get("PathHealthy") == "True",
            "queue_depth": queue_depth(),
            "queue_limit": _QUEUE_LIMIT,
        }
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
