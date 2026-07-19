"""Dashboard home: a single-glance overview across agent health + kanban + ITIL + CMDB.

One aggregate call (``/api/overview``) powers the ``/`` landing page. Keeps the
agent-health / active-tasks / recent-activity glance and adds operational summary
tiles that deep-link to the board, cockpit, and CMDB.
"""
from __future__ import annotations

from pathlib import Path

_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def get_overview_home(home: Path) -> dict:
    from . import dashboard_cmdb as dc
    from . import dashboard_itil as di
    from .card import LANE_ORDER, KanbanBoard

    kb = KanbanBoard(home)
    grid = kb.grid()
    wip = kb.wip_report()
    cols = ("backlog", "ready", "doing", "review", "done")

    # active work = in-flight cards (doing + review), what the operator watches
    active_tasks = []
    for lane in LANE_ORDER:
        for col in ("doing", "review"):
            for c in grid[lane][col]:
                run = c.meta.get("agent_run") or {}
                active_tasks.append({
                    "id": c.id, "title": c.title, "kind": c.kind.value,
                    "status": c.status.value, "owner": c.owner,
                    "priority": c.priority, "swimlane": c.swimlane,
                    "ai": run.get("state"),
                })
    active_tasks.sort(key=lambda t: _PRIORITY_RANK.get(t["priority"], 2))

    by_column = {col: sum(len(grid[lane][col]) for lane in LANE_ORDER) for col in cols}
    over = [col for col, st in wip.items() if st.get("over")]

    try:
        itil = di.get_overview(home)
    except Exception:  # noqa: BLE001
        itil = {}
    try:
        cmdb = dc.get_overview(home)
    except Exception:  # noqa: BLE001
        cmdb = {"total": 0, "health": {}}

    agent = {}
    try:
        from .dashboard import _get_agent_status
        st = _get_agent_status(home)
        agent = {
            "name": st.get("name"),
            "pillars": st.get("pillars", {}),
            "memory": st.get("memory", {}),
            "consciousness": st.get("consciousness", {}),
        }
    except Exception:  # noqa: BLE001
        pass

    return {
        "agent": agent,
        "kanban": {
            "active": sum(by_column.values()),
            "by_column": by_column,
            "wip_over": over,
        },
        "active_tasks": active_tasks[:12],
        "itil": {
            "kpis": itil.get("kpis", {}),
            "breaches": len([b for b in itil.get("breach_risk", []) if b.get("over")]),
            "cab": len(itil.get("cab_queue", [])),
        },
        "cmdb": {"total": cmdb.get("total", 0), "health": cmdb.get("health", {})},
        "activity": itil.get("activity", [])[:8],
    }
