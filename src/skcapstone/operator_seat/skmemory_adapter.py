"""skmemory operator adapter: Atlas manages skmemory too (the third app adapter).

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skmemory plugs in by exposing the same three verbs the fleet does. The health
probe is injectable so tests never touch a live skmemory.

The default probe DELEGATES to skmemory's own operator-facet contract
(``skmemory.operator_probe``, the exact module ``skmemory operator observe``
runs) instead of maintaining a second, independent signal reader here. This is a
fix for card 504d0046 (ATLAS Eyes PR #178 first run): the old default probe
shelled out to ``skmemory daemon status``, a subcommand that does not exist on
the installed CLI (exit 2, "no such command"), which read as confidently WRONG
``EmbedServing=False`` / ``ReconcileFresh=False`` no matter the real embed
backend or index-freshness state. Delegating to the real, tested probe (the
embedding-backend health check and the local index-age check) makes this ONE
real signal with two callers (in-process seat, out-of-process cli), so the two
lanes cannot drift again short of the underlying probe itself changing behavior
mid-flight. Fails SAFE (healthy) when skmemory is not importable or the probe
raises, so an inability to probe never raises a false alarm.
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
    """Best-effort skmemory health, delegated to ``skmemory.operator_probe`` (the
    same real-signal module the ``skmemory operator observe`` cli lane runs).
    Fails SAFE (healthy) when skmemory is not importable or the probe raises, so
    an inability to probe never raises a false alarm."""
    try:
        from skmemory.operator_probe import observe as _skmemory_operator_observe

        by_type = {c["type"]: c["status"] for c in _skmemory_operator_observe()["conditions"]}
        return {
            "embed_serving": by_type.get("EmbedServing") == "True",
            "reconcile_fresh": by_type.get("ReconcileFresh") == "True",
        }
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
