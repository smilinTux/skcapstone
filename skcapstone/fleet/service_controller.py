"""ServiceController (spec 5.2): place-once, conservative failover, drift.

Runs on the control-plane node (a tick wired in Task 6). It requests
placements via the Phase 2 scheduler and emits events; it never writes
status (sknoded-owned) and never edits spec (operator-owned). Failover
defaults to manual: node-Dead fires one deduped alert and moves nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import alerts, events, scheduler, store
from .node_controller import NodeView, node_views
from .paths import FleetPaths
from .services import ServiceSpecError, normalize_service_spec, service_workload


@dataclass(frozen=True)
class ServiceRow:
    """One row of skfleet services (read-time merge, nothing persisted)."""

    name: str
    node: str | None
    state: str
    ready: str
    paused: bool
    stale: bool


def _ready_from(status: dict | None) -> str:
    if status is None or status.get("stale"):
        return "Unknown"
    for cond in status.get("conditions", []):
        if cond.get("type") == "Ready":
            return str(cond.get("status", "Unknown"))
    return "Unknown"


def service_rows(paths: FleetPaths) -> list[ServiceRow]:
    """All Services with placement, observed state, and staleness flags."""
    rows: list[ServiceRow] = []
    for payload in store.list_specs(paths, "service"):
        name = payload["name"]
        merged = store.merged(paths, "service", name) or {}
        placement = merged.get("placement")
        target = placement.get("node") if placement else None
        status = None
        for st in merged.get("statuses", []):
            if target is None or st.get("node") == target:
                status = st
                break
        state = (
            "unobserved"
            if status is None
            else str(status.get("status", {}).get("state", "unknown"))
        )
        if status is not None and status.get("stale"):
            state = "unobserved" if state == "unobserved" else state
        rows.append(
            ServiceRow(
                name=name,
                node=target,
                state=state,
                ready=_ready_from(status),
                paused=bool(payload.get("spec", {}).get("paused", False)),
                stale=bool(status.get("stale")) if status else False,
            )
        )
    return rows


def reconcile_once(
    paths: FleetPaths,
    *,
    node: str,
    views: list[NodeView] | None = None,
    alert: Callable[..., bool] = alerts.send_alert,
) -> dict:
    """One controller pass: place the unplaced, watch the placed.

    Placement policy (R4, deliberately conservative):
    - no placement yet: request one via scheduler.place (freeze-gated there)
    - placement exists and its node is not Dead: keep, always
    - placement node Dead + failover auto: re-place (feasible() excludes
      the Dead node, placementGeneration bumps)
    - placement node Dead + failover manual: one deduped alert, no writes
    """
    views = node_views(paths) if views is None else views
    phases = {v.name: v.phase for v in views}
    sched = store.Writer(role="scheduler", node=node, identity=store.writer_identity())
    ctrl = store.Writer(role="controller", node=node, identity=store.writer_identity())
    out: dict = {"placed": [], "failovers": [], "alerted": [], "kept": [], "skipped": []}
    for payload in store.list_specs(paths, "service"):
        name = payload["name"]
        try:
            spec = normalize_service_spec(payload.get("spec", {}))
        except ServiceSpecError as exc:
            events.emit(
                paths,
                ctrl,
                kind="service",
                name=name,
                type="Config",
                reason="SpecInvalid",
                message=str(exc),
            )
            out["skipped"].append(name)
            continue
        if spec["deleted"]:
            out["skipped"].append(name)
            continue
        workload = service_workload(payload)
        existing = store.read_placement(paths, "service", name)
        if existing is None:
            placed = scheduler.place(paths, workload, writer=sched, views=views)
            if placed is not None:
                out["placed"].append(name)
            continue
        phase = phases.get(existing.get("node"), "Dead")
        if phase != "Dead":
            out["kept"].append(name)
            continue
        if spec["failover"] == "auto":
            placed = scheduler.place(paths, workload, writer=sched, views=views)
            if placed is not None and placed.get("node") != existing.get("node"):
                out["failovers"].append(name)
            else:
                out["kept"].append(name)
            continue
        message = (
            f"service {name} is placed on {existing.get('node')} which is "
            f"Dead; failover=manual, no automatic re-place (move it with "
            f"skfleet or set failover: auto)"
        )
        if events.emit(
            paths,
            ctrl,
            kind="service",
            name=name,
            type="Failover",
            reason="NodeDead",
            message=message,
        ):
            alert(f"fleet: {message}", level="error")
        out["alerted"].append(name)
    return out


def node_residents(paths: FleetPaths, node: str) -> list[dict]:
    """Services placed on or observed on one node (the drain inventory).

    Placements are desired state; observed statuses catch legacy residents
    that predate fleet management. Deduped by name, placement wins.
    """
    residents: dict[str, dict] = {}
    service_dir = paths.node_status_dir(node) / "service"
    if service_dir.exists():
        for status_file in sorted(service_dir.glob("*.json")):
            name = status_file.stem
            st = store.read_status(paths, "service", name, node)
            state = str((st or {}).get("status", {}).get("state", "unknown"))
            residents[name] = {"name": name, "via": "status", "state": state}
    for placement in store.list_placements(paths, "service"):
        if placement.get("node") != node:
            continue
        name = placement["name"]
        st = store.read_status(paths, "service", name, node)
        state = str((st or {}).get("status", {}).get("state", "unobserved"))
        residents[name] = {"name": name, "via": "placement", "state": state}
    return [residents[k] for k in sorted(residents)]
