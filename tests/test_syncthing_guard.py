"""Simulation tests for bounded Syncthing fleet recovery and alerting."""

from __future__ import annotations

import json
from pathlib import Path

from skcoord.cmdb import CIType, make_ci_id
from skcoord.itil import ITILManager

from skcapstone.syncthing_guard import inspect_node, reconcile_incidents


def probe_payload(*, available: bool = True, pending: int = 0, errors: int = 0) -> dict:
    return {
        "schema": 1,
        "configured": True,
        "available": available,
        "version": "v2.1.3",
        "config_schema": 52,
        "system_errors": errors,
        "connected_devices": 9,
        "ports": [8384, 22000] if available else [],
        "folders": [
            {
                "id": "skcapstone",
                "state": "syncing" if pending else "idle",
                "paused": False,
                "pending_items": pending,
                "pull_errors": 0,
            }
        ],
    }


def unit_state(active: str, *, enabled: str = "enabled") -> str:
    sub = "running" if active == "active" else "dead"
    return (
        f"LoadState=loaded\nActiveState={active}\nSubState={sub}\n"
        f"UnitFileState={enabled}\nNRestarts=0\n"
    )


class GuardRunner:
    def __init__(self, states: list[str], payload: dict, start_ok: bool = True) -> None:
        self.host = "chiap04"
        self.states = list(states)
        self.payload = payload
        self.start_ok = start_ok
        self.calls: list[list[str]] = []

    def run(self, argv):
        self.calls.append(list(argv))
        command = " ".join(argv)
        if "systemctl --user show" in command:
            return self.states.pop(0)
        if "systemctl --user start" in command:
            return "" if self.start_ok else None
        if argv[:2] == ["python3", "-c"]:
            return json.dumps(self.payload)
        return None


def test_inactive_enabled_unit_is_started_once_without_config_mutation() -> None:
    runner = GuardRunner([unit_state("inactive"), unit_state("active")], probe_payload())

    row = inspect_node(runner)

    assert row["action"] == "started"
    assert row["failure"] == ""
    assert sum("systemctl --user start" in " ".join(call) for call in runner.calls) == 1
    assert all(
        "systemctl --user enable" not in " ".join(call)
        and "systemctl --user restart" not in " ".join(call)
        for call in runner.calls
    )


def test_failed_start_is_reported_and_never_loops_or_restarts() -> None:
    runner = GuardRunner(
        [unit_state("failed"), unit_state("failed")],
        probe_payload(),
        start_ok=False,
    )

    row = inspect_node(runner)

    assert row["action"] == "start_attempted"
    assert row["failure"] == "start_failed"
    assert len([call for call in runner.calls if "systemctl --user start" in " ".join(call)]) == 1
    assert all("systemctl --user restart" not in " ".join(call) for call in runner.calls)


def test_disabled_unit_fails_closed_without_start() -> None:
    runner = GuardRunner([unit_state("inactive", enabled="disabled")], probe_payload())
    row = inspect_node(runner)
    assert row["failure"] == "unit_not_enabled"
    assert all("systemctl --user start" not in " ".join(call) for call in runner.calls)


def test_unchanged_pending_work_is_stalled_without_restart() -> None:
    runner = GuardRunner([unit_state("active")], probe_payload(pending=4))
    row = inspect_node(runner, {"health": "syncing", "pending_items": 4})
    assert row["failure"] == "stalled"
    assert row["pending_items"] == 4
    assert all(
        "systemctl --user start" not in " ".join(call)
        and "systemctl --user restart" not in " ".join(call)
        for call in runner.calls
    )


def test_healthy_and_active_but_unavailable_cases_are_distinct() -> None:
    healthy = inspect_node(GuardRunner([unit_state("active")], probe_payload()))
    unavailable = inspect_node(GuardRunner([unit_state("active")], probe_payload(available=False)))
    assert healthy["health"] == "healthy"
    assert healthy["failure"] == ""
    assert unavailable["health"] == "down"
    assert unavailable["failure"] == "probe_unavailable"


def test_persistent_failure_resolves_on_recovery_and_reopens_on_recurrence(
    tmp_path: Path,
) -> None:
    node = "chiap04"
    service_id = make_ci_id(CIType.SERVICE.value, f"syncthing@{node}")
    failed = {
        "nodes": [{"node": node, "failure": "start_failed"}],
        "failures": 1,
        "recoveries": 0,
    }
    prior = [{"syncthing_guard": failed}]

    first = reconcile_incidents(tmp_path, failed, [], threshold=2, agent="jarvis")
    second = reconcile_incidents(tmp_path, failed, prior, threshold=2, agent="jarvis")
    repeated = reconcile_incidents(tmp_path, failed, prior, threshold=2, agent="jarvis")

    assert first == []
    assert second == repeated
    incidents = ITILManager(tmp_path).list_incidents(service=service_id)
    assert len(incidents) == 1

    recovered = {"nodes": [{"node": node, "failure": ""}]}
    assert reconcile_incidents(tmp_path, recovered, prior, threshold=2, agent="jarvis") == [
        incidents[0].id
    ]
    manager = ITILManager(tmp_path)
    resolved = manager.list_incidents(service=service_id)[0]
    assert resolved.status.value == "resolved"
    assert resolved.resolution_summary == "Resolved after a healthy current Syncthing guard check"
    assert manager.find_open_incident_for_service(service_id) is None

    assert reconcile_incidents(tmp_path, failed, prior, threshold=2, agent="jarvis") == [
        incidents[0].id
    ]
    reopened = manager.find_open_incident_for_service(service_id)
    assert reopened is not None
    assert reopened.status.value == "investigating"
    assert reopened.resolution_summary is None


def test_healthy_guard_does_not_resolve_manual_incident(tmp_path: Path) -> None:
    node = "chiap04"
    service_id = make_ci_id(CIType.SERVICE.value, f"syncthing@{node}")
    manager = ITILManager(tmp_path)
    incident = manager.create_incident(
        title="Manual Syncthing investigation",
        affected_services=[service_id],
    )

    assert (
        reconcile_incidents(
            tmp_path,
            {"nodes": [{"node": node, "failure": ""}]},
            [],
            threshold=2,
            agent="jarvis",
        )
        == []
    )
    assert manager.list_incidents(service=service_id)[0].id == incident.id
    assert manager.list_incidents(service=service_id)[0].status.value == "detected"


def test_single_failure_after_recovery_does_not_reopen_before_threshold(tmp_path: Path) -> None:
    node = "chiap04"
    failed = {"nodes": [{"node": node, "failure": "start_failed"}]}
    service_id = make_ci_id(CIType.SERVICE.value, f"syncthing@{node}")
    manager = ITILManager(tmp_path)
    incident = manager.create_incident(
        title="Syncthing start failed on chiap04",
        source="service_health",
        affected_services=[service_id],
        failure_class="start_failed",
    )
    manager.update_incident(incident.id, "jarvis", new_status="resolved")

    assert reconcile_incidents(tmp_path, failed, [], threshold=2, agent="jarvis") == []
    assert manager.list_incidents(service=service_id)[0].status.value == "resolved"
