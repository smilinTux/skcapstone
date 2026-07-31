"""skdashboard operator adapter: Atlas manages the coordination dashboard too.

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skdashboard plugs in by exposing the same three verbs the fleet does.

The observe probes are REAL and injectable (tests never touch a live dashboard):
the dashboard's ``/api/status`` endpoint (DashboardReady) and its ``/api/board``
endpoint (BoardReadable, the coordination board loads without an error). Both
probes fail SAFE (report healthy) rather than raising a false alarm when the
dashboard is unreachable.

The act verb (`skdashboard_act`) maps the one reversible standard action
(restart-dashboard) onto the tested `actuator.honor` systemd path, refusing when
the fleet is frozen.
"""

from __future__ import annotations

import os

from ..fleet import store
from . import actuator

#: The order MUST match the skdashboard manifest's operator.conditions exactly;
#: the drift-guard test asserts it.
CONDITIONS = [
    "DashboardReady",
    "BoardReadable",
]

#: skdashboard conditions are health-type (they fire when status is False), so
#: they are NOT problem-when-true; the operator brief treats them correctly.
_ACTIONS = [
    {
        "name": "restart-dashboard",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the skcapstone dashboard web server and verify DashboardReady",
        "kedb_refs": ["ke-skdashboard-down"],
    },
]

#: The dashboard's default served port (skcapstone.dashboard.DEFAULT_DASHBOARD_PORT).
_DASHBOARD_STATUS_URL = "http://127.0.0.1:7778/api/status"
_DASHBOARD_BOARD_URL = "http://127.0.0.1:7778/api/board"
_UNIT_RESTART_DASHBOARD = "skcapstone-dashboard.service"


def _b(value: bool) -> str:
    return "True" if value else "False"


# --- real signal readers (each fails safe = healthy) -------------------------


def _get_json(url: str, timeout: float = 5.0):
    """Best-effort JSON GET. Returns the decoded body, or None on any failure."""
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _default_probe() -> dict:
    """Best-effort skdashboard health from real signals. Fails SAFE (healthy) when
    the dashboard is unreachable, so an inability to probe never raises a false
    alarm.

    DashboardReady: ``/api/status`` answers with a non-error body.
    BoardReadable: ``/api/board`` answers with a body that carries no ``error``
    key (the ``_get_board_state`` helper reports failures as ``{"error": ...}``).
    """
    status_url = os.environ.get("SKDASHBOARD_STATUS_URL", _DASHBOARD_STATUS_URL)
    board_url = os.environ.get("SKDASHBOARD_BOARD_URL", _DASHBOARD_BOARD_URL)

    status = _get_json(status_url)
    board = _get_json(board_url)

    # Unreachable (None) fails safe to ready. A returned body with an "error" key
    # is a real failure and reads as down.
    dashboard_ready = status is None or not (
        isinstance(status, dict) and status.get("error")
    )
    board_readable = board is None or not (
        isinstance(board, dict) and board.get("error")
    )
    return {"dashboard_ready": dashboard_ready, "board_readable": board_readable}


# --- contract verbs ----------------------------------------------------------


def skdashboard_explain() -> dict:
    """skdashboard's self-description in the adapter-contract shape."""
    return {
        "kinds": ["dashboard", "board"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skdashboard_observe(probe=None) -> dict:
    """Read-only skdashboard health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    return {
        "conditions": [
            {
                "type": "DashboardReady",
                "status": _b(bool(st.get("dashboard_ready", True))),
                "object": "skcapstone-dashboard",
            },
            {
                "type": "BoardReadable",
                "status": _b(bool(st.get("board_readable", True))),
                "object": "coordination-board",
            },
        ]
    }


def skdashboard_act(
    paths, proposal: dict, classification: dict, *, runner=None
) -> dict:
    """Apply a reversible standard skdashboard action via the tested systemd path.

    Maps restart-dashboard onto `actuator.honor` (the same `systemctl --user
    restart` verb the fleet uses), refusing when the fleet is frozen. Any other
    action raises: it never reached the manifest's proposedStandardActions.
    """
    if store.is_frozen(paths):
        raise RuntimeError("fleet is frozen: the operator does not actuate")
    action = proposal.get("action")
    if action != "restart-dashboard":
        raise ValueError(f"no ops-channel mapping for skdashboard action {action!r}")
    honor_action = {"action": "restart_service", "ts": proposal.get("ts")}
    return actuator.honor(paths, honor_action, _UNIT_RESTART_DASHBOARD, runner=runner)


#: A loop-compatible observe (paths, now_iso) -> {conditions}; ignores both.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skdashboard_observe()


__all__ = [
    "skdashboard_explain",
    "skdashboard_observe",
    "skdashboard_act",
    "observe",
    "CONDITIONS",
]
