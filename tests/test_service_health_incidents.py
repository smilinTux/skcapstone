"""service_health incident behavior — no recurring-note churn (prb-7810b08e)."""
from pathlib import Path

import pytest

import skcapstone
import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.itil import ITILManager
from skcapstone.service_health import _create_incident_for_down_service


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch) -> None:
    """Redirect ITIL + GTD storage to a tmp dir (no ~/.skcapstone writes)."""
    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))


def test_repeated_down_creates_one_incident_with_no_still_down_notes(tmp_path: Path):
    result = {"name": "skvector", "status": "down", "error": "no route to host"}

    # Three consecutive health cycles while the service stays down.
    _create_incident_for_down_service(result)
    _create_incident_for_down_service(result)
    _create_incident_for_down_service(result)

    mgr = ITILManager(str(tmp_path))
    incidents = [i for i in mgr.list_incidents() if "skvector" in i.affected_services]

    # Exactly one incident — no duplicates from repeated cycles.
    assert len(incidents) == 1
    # And the timeline never accumulated recurring "still down" churn.
    still_down = [e for e in incidents[0].timeline if "still down" in (e.get("note") or "")]
    assert still_down == []
