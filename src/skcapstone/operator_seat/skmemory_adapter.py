"""skmemory operator adapter: Atlas manages skmemory too (the third app adapter).

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skmemory plugs in by exposing the same three verbs the fleet does. The health
probe is injectable so tests never touch a live skmemory; the default reads the
skmemory daemon status and fails safe (reports healthy) rather than raising a
false alarm when skmemory cannot be reached.
"""

from __future__ import annotations

from typing import Callable

CONDITIONS = ["EmbedServing", "ReconcileFresh"]

#: skmemory health conditions are health-type (they fire when status is False), so
#: they are NOT problem-when-true; the operator brief treats them correctly by
#: default.
_ACTIONS = [
    {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the wedged skmemory service",
        "kedb_refs": [],
    },
    {
        "name": "reindex",
        "standard": False,
        "reversible": True,
        "blast_radius": "medium",
        "runbook": "rebuild the skmemory embedding index (major)",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


def _default_probe() -> dict:
    """Best-effort skmemory health. Fails SAFE (healthy) when skmemory is
    unreachable, so an inability to probe never raises a false alarm."""
    try:
        import subprocess

        r = subprocess.run(
            ["skmemory", "daemon", "status"], capture_output=True, text=True, timeout=10
        )
        alive = r.returncode == 0
        return {"embed_serving": alive, "reconcile_fresh": alive}
    except Exception as exc:
        return {"_probe_error": type(exc).__name__}


def skmemory_explain() -> dict:
    """skmemory's self-description in the adapter-contract shape."""
    return {
        "kinds": ["embed", "reconcile"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skmemory_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skmemory health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    unknown = bool(st.get("_probe_error"))
    return {
        "conditions": [
            {
                "type": "EmbedServing",
                "status": "Unknown" if unknown else _b(bool(st.get("embed_serving"))),
                "object": "embed-service",
            },
            {
                "type": "ReconcileFresh",
                "status": "Unknown" if unknown else _b(bool(st.get("reconcile_fresh"))),
                "object": "reconciler",
            },
        ]
    }


#: A loop-compatible observe (name, now_iso) -> {conditions}; ignores now_iso.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skmemory_observe()


__all__ = ["skmemory_explain", "skmemory_observe", "observe", "CONDITIONS"]
