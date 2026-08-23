"""AgentController (Phase 5 step 2): read-time Agent rows.

Runs on the control-plane node, mirroring CronController's read-time
conventions. Read-time only: never writes status (the agent's daemon owns
its own status.observed) and never edits spec (operator-owned). Convergence
(soul/model/daemon writes) is a later card (5.2); until then an Agent's
observed state is whatever status.observed (if any) already exists for it.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .agent import AgentSpecError, agent_conditions, normalize_agent_spec
from .paths import FleetPaths

READY_CONDITIONS = ("SoulLoaded", "ModelRoutable", "DaemonReady")


@dataclass(frozen=True)
class AgentRow:
    """One row of skfleet get agents (read-time derivation, nothing persisted)."""

    name: str
    node: str | None
    soul: str | None
    model: str | None
    ready: str


def _first_status(merged: dict) -> dict | None:
    statuses = merged.get("statuses", [])
    return statuses[0] if statuses else None


def agent_rows(paths: FleetPaths, now_iso: str) -> list[AgentRow]:
    """All Agents with observed soul/model/daemon state and drift conditions."""
    rows: list[AgentRow] = []
    for payload in store.list_specs(paths, "agent"):
        name = payload["name"]
        if payload.get("spec", {}).get("deleted"):
            continue
        try:
            spec = normalize_agent_spec(payload.get("spec", {}))
        except AgentSpecError:
            continue
        merged = store.merged(paths, "agent", name) or {}
        status = _first_status(merged)
        observed = ((status or {}).get("status") or {}).get("observed") or {}
        conds = {c["type"]: c["status"] for c in agent_conditions(spec, observed, now_iso)}
        ready = "True" if all(conds.get(t) == "True" for t in READY_CONDITIONS) else "False"
        rows.append(
            AgentRow(
                name=name,
                node=(status or {}).get("node"),
                soul=observed.get("active_soul"),
                model=observed.get("model"),
                ready=ready,
            )
        )
    return rows
