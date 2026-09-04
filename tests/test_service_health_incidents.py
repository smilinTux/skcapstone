"""service_health incident behavior - no recurring-note churn (prb-7810b08e)."""

from pathlib import Path

import pytest

import skcapstone
import skcapstone.mcp_tools._helpers as _helpers
from skcapstone import service_health
from skcapstone.itil import ITILManager
from skcapstone.service_health import (
    _auto_resolve_recovered_service,
    _create_incident_for_down_service,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Redirect ITIL + GTD storage to a tmp dir (no ~/.skcapstone writes)."""
    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setattr(service_health, "_may_file_incidents", lambda: True)


def test_repeated_down_creates_one_incident_with_no_still_down_notes(tmp_path: Path):
    result = {"name": "skvector", "status": "down", "error": "no route to host"}

    # Three consecutive health cycles while the service stays down.
    _create_incident_for_down_service(result)
    _create_incident_for_down_service(result)
    _create_incident_for_down_service(result)

    mgr = ITILManager(str(tmp_path))
    incidents = [i for i in mgr.list_incidents() if "skvector" in i.affected_services]

    # Exactly one incident - no duplicates from repeated cycles.
    assert len(incidents) == 1
    # And the timeline never accumulated recurring "still down" churn.
    still_down = [e for e in incidents[0].timeline if "still down" in (e.get("note") or "")]
    assert still_down == []


def test_current_healthy_probe_resolves_sev3_incident(tmp_path: Path):
    result = {
        "name": "skvector",
        "status": "down",
        "error": "no route to host",
        "endpoint": "https://skvector.example/healthz",
        "vantage_point": "any",
        "probed_from": "chiap04",
    }
    _create_incident_for_down_service(result)

    _auto_resolve_recovered_service(
        {
            "name": "skvector",
            "status": "up",
            "endpoint": "https://skvector.example/healthz",
            "vantage_point": "any",
            "probed_from": "chiap04",
        }
    )

    incident = ITILManager(tmp_path).list_incidents(service="skvector")[0]
    assert incident.status.value == "resolved"
    assert incident.resolution_summary == "Resolved after a successful current health probe"
    assert "address=https://skvector.example/healthz" in incident.impact
    assert "probed_from=chiap04" in incident.impact
    assert "declared_vantage=any" in incident.impact
    assert "address=https://skvector.example/healthz" in incident.timeline[-1]["note"]
    assert "probed_from=chiap04" in incident.timeline[-1]["note"]


def test_recurring_failure_reopens_same_deduplicated_incident(tmp_path: Path):
    result = {"name": "skvector", "status": "down", "error": "no route to host"}
    _create_incident_for_down_service(result)
    manager = ITILManager(tmp_path)
    first = manager.list_incidents(service="skvector")[0]
    _auto_resolve_recovered_service({"name": "skvector", "status": "up"})

    _create_incident_for_down_service(result)

    incidents = manager.list_incidents(service="skvector")
    assert len(incidents) == 1
    assert incidents[0].id == first.id
    assert incidents[0].status.value == "investigating"
    assert incidents[0].resolution_summary is None


def test_down_status_cannot_resolve_incident(tmp_path: Path):
    result = {"name": "skvector", "status": "down", "error": "no route to host"}
    _create_incident_for_down_service(result)

    _auto_resolve_recovered_service(result)

    assert ITILManager(tmp_path).list_incidents(service="skvector")[0].status.value == "detected"


def test_manual_incident_is_not_auto_resolved(tmp_path: Path) -> None:
    manager = ITILManager(tmp_path)
    incident = manager.create_incident(title="skvector degraded", affected_services=["skvector"])

    _auto_resolve_recovered_service({"name": "skvector", "status": "up"})

    assert manager.list_incidents(service="skvector")[0].id == incident.id
    assert manager.list_incidents(service="skvector")[0].status.value == "detected"


def test_non_authority_healthy_probe_does_not_resolve_incident(
    tmp_path: Path, monkeypatch
) -> None:
    result = {"name": "skvector", "status": "down", "error": "no route to host"}
    _create_incident_for_down_service(result)
    monkeypatch.setattr(service_health, "_may_file_incidents", lambda: False)

    _auto_resolve_recovered_service({"name": "skvector", "status": "up"})

    assert ITILManager(tmp_path).list_incidents(service="skvector")[0].status.value == "detected"
