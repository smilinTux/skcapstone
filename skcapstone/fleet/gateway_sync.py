"""Gateway upstream health map (Phase 6 step 2, planning half).

Pure transform from ModelServer rows to the upstream-health payload
skgateway consumes. Feeds health only; per spec rule 5.6, the gateway
routes and the controller only reports health, so this module never makes
routing decisions. No network call here (that is 6.2b apply).
"""

from __future__ import annotations

from .modelserver_controller import ModelServerRow


def gateway_upstream_health(rows: list[ModelServerRow]) -> dict:
    """Map ModelServer rows to the upstream-health payload skgateway consumes.

    Args:
        rows: ModelServer rows from modelserver_controller.modelserver_rows.

    Returns:
        A dict keyed by row name, each value holding serving, ports, vram,
        and node for that row.
    """
    return {
        row.name: {
            "serving": bool(row.serving),
            "ports": list(row.ports),
            "vram": row.vram,
            "node": row.node,
        }
        for row in rows
    }
