"""Bounded fleet guard for required Syncthing user services.

The guard may start an already loaded and enabled unit. It never enables a
unit, restarts an active process, or changes Syncthing folder and device
configuration. Application health comes from the secret-free CMDB collector.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable

from skcoord.cmdb import CIType, make_ci_id
from skcoord.discovery_syncthing import collect_syncthing_health
from skcoord.itil import ITILManager

_APPROVED_ENABLED_STATES = frozenset({"enabled", "enabled-runtime", "static"})
_USER_BUS = (
    'uid="$(id -u)"; '
    'export XDG_RUNTIME_DIR="/run/user/$uid"; '
    'export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus"; '
)
_SHOW = [
    "sh",
    "-c",
    _USER_BUS + "exec systemctl --user show syncthing.service "
    "--property=LoadState --property=ActiveState --property=SubState "
    "--property=UnitFileState --property=NRestarts --no-pager",
]
_START = [
    "sh",
    "-c",
    _USER_BUS + "exec systemctl --user start syncthing.service",
]


def _unit_state(runner) -> dict[str, str] | None:
    output = runner.run(_SHOW)
    if output is None:
        return None
    return {
        key: value
        for line in output.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _previous_row(artifacts: Sequence[Mapping[str, object]], node: str) -> dict:
    for artifact in artifacts:
        guard = artifact.get("syncthing_guard")
        if not isinstance(guard, Mapping):
            continue
        rows = guard.get("nodes")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("node") == node:
                return row
        return {}
    return {}


def inspect_node(runner, previous: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Inspect one node and perform only an approved stopped-unit start."""
    node = runner.host
    before = _unit_state(runner)
    row: dict[str, Any] = {
        "node": node,
        "unit": "syncthing.service",
        "action": "none",
        "failure": "",
        "active_state": "unknown",
        "sub_state": "unknown",
        "unit_file_state": "unknown",
        "restarts": 0,
        "health": "unknown",
        "pending_items": 0,
        "pull_errors": 0,
        "system_errors": 0,
    }
    if before is None:
        row["failure"] = "transport_unavailable"
        return row

    load_state = before.get("LoadState", "unknown")
    active_state = before.get("ActiveState", "unknown")
    unit_file_state = before.get("UnitFileState", "unknown")
    if (
        load_state == "loaded"
        and unit_file_state in _APPROVED_ENABLED_STATES
        and active_state in {"inactive", "failed"}
    ):
        row["action"] = "start_attempted"
        runner.run(_START)
        after = _unit_state(runner)
        if after is not None:
            before = after
            active_state = after.get("ActiveState", "unknown")
        if active_state == "active":
            row["action"] = "started"

    row.update(
        active_state=before.get("ActiveState", "unknown"),
        sub_state=before.get("SubState", "unknown"),
        unit_file_state=before.get("UnitFileState", "unknown"),
    )
    try:
        row["restarts"] = max(0, int(before.get("NRestarts", "0") or 0))
    except ValueError:
        row["restarts"] = 0

    if load_state != "loaded":
        row["failure"] = "unit_missing"
        return row
    if unit_file_state not in _APPROVED_ENABLED_STATES:
        row["failure"] = "unit_not_enabled"
        return row
    if row["active_state"] != "active":
        row["failure"] = "start_failed" if row["action"] == "start_attempted" else "unit_down"
        return row

    items = collect_syncthing_health(runner)
    service = next((item for item in items if item.ci_type == CIType.SERVICE.value), None)
    if service is None:
        row["failure"] = "probe_unavailable"
        return row
    attributes = service.attributes
    row.update(
        health=attributes.get("sync_health_state", "unknown"),
        pending_items=int(attributes.get("sync_pending_items") or 0),
        pull_errors=int(attributes.get("sync_pull_errors") or 0),
        system_errors=int(attributes.get("sync_system_errors") or 0),
    )
    if row["health"] == "down":
        row["failure"] = "probe_unavailable"
    elif row["health"] == "degraded":
        row["failure"] = "sync_errors"
    elif (
        row["health"] == "syncing"
        and row["pending_items"] > 0
        and previous
        and previous.get("health") == "syncing"
        and previous.get("pending_items") == row["pending_items"]
    ):
        row["failure"] = "stalled"
    return row


def inspect_fleet(
    targets: Sequence[str],
    runner_factory: Callable,
    artifacts: Sequence[Mapping[str, object]],
) -> dict[str, Any]:
    """Inspect exact configured targets and return bounded artifact evidence."""
    rows = []
    for node in targets:
        try:
            rows.append(inspect_node(runner_factory(node), _previous_row(artifacts, node)))
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "node": node,
                    "unit": "syncthing.service",
                    "action": "none",
                    "failure": type(exc).__name__,
                    "active_state": "unknown",
                    "sub_state": "unknown",
                    "unit_file_state": "unknown",
                    "restarts": 0,
                    "health": "unknown",
                    "pending_items": 0,
                    "pull_errors": 0,
                    "system_errors": 0,
                }
            )
    return {
        "policy": "start-loaded-enabled-only",
        "nodes": rows,
        "failures": sum(bool(row["failure"]) for row in rows),
        "recoveries": sum(row["action"] == "started" for row in rows),
    }


def _consecutive_failures(
    artifacts: Sequence[Mapping[str, object]], node: str, failure: str
) -> int:
    count = 1
    for artifact in artifacts:
        row = _previous_row([artifact], node)
        if not row or row.get("failure") != failure:
            break
        count += 1
    return count


def reconcile_incidents(
    home,
    guard: Mapping[str, object],
    artifacts: Sequence[Mapping[str, object]],
    *,
    threshold: int,
    agent: str,
) -> list[str]:
    """Create one persistent-failure incident and resolve verified recovery."""
    manager = ITILManager(home)
    incident_ids: list[str] = []
    rows = guard.get("nodes") or []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        node = str(row.get("node") or "unknown")
        service_id = make_ci_id(CIType.SERVICE.value, f"syncthing@{node}")
        failure = str(row.get("failure") or "")
        existing = manager.find_open_incident_for_service(service_id)
        if not failure:
            if existing is not None and existing.created_by == "syncthing_guard":
                manager.update_incident(
                    existing.id,
                    agent,
                    new_status="resolved",
                    note=f"Syncthing recovered on {node}; verified by the current fleet guard run",
                    resolution_summary="Resolved after a healthy current Syncthing guard check",
                )
                incident_ids.append(existing.id)
            continue
        required = 1 if failure == "stalled" else max(1, threshold)
        if _consecutive_failures(artifacts, node, failure) < required:
            continue
        if existing is None:
            incident = manager.create_incident(
                title=f"Syncthing {failure.replace('_', ' ')} on {node}",
                severity="sev3",
                source="service_health",
                affected_services=[service_id],
                impact=f"Persistent Syncthing guard failure on {node}: {failure}",
                managed_by=agent,
                created_by="syncthing_guard",
                tags=["auto-detected", "syncthing", "fleet-guard", node],
                failure_class=failure,
            )
            existing = incident
        incident_ids.append(existing.id)
    return sorted(set(incident_ids))
