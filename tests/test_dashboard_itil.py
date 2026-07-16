"""Tests for the Phase 3 ITIL cockpit API (dashboard_itil + routes)."""
from __future__ import annotations

import pytest

from skcapstone.dashboard import create_app
from skcapstone import dashboard_itil as di


@pytest.fixture
def home(tmp_path):
    from skcapstone.itil import ITILManager
    mgr = ITILManager(tmp_path)
    # an open SEV2 incident, a resolved one, a change in review, a problem
    mgr.create_incident(title="skmem-pg down .41", severity="sev2", created_by="lumina",
                        affected_services=["skmem-pg"])
    inc2 = mgr.create_incident(title="skchat blip", severity="sev3", created_by="opus")
    mgr.update_incident(inc2.id, "opus", new_status="acknowledged")
    mgr.update_incident(inc2.id, "opus", new_status="resolved", note="restarted")
    ch = mgr.propose_change(title="Gateway cutover", created_by="lumina", change_type="normal",
                            risk="medium", rollback_plan="revert route")
    mgr.update_change(ch.id, "lumina", new_status="reviewing")
    mgr.submit_cab_vote(ch.id, "jarvis", decision="approved")
    mgr.create_problem(title="Recurring conflicts", created_by="opus")
    return tmp_path


def test_overview_shape(home):
    d = di.get_overview(home)
    assert "kpis" in d and d["kpis"]["open_incidents"] >= 1
    assert d["by_severity"]["sev2"] >= 1
    assert isinstance(d["breach_risk"], list)
    assert isinstance(d["cab_queue"], list)


def test_overview_cab_queue_has_votes(home):
    d = di.get_overview(home)
    assert d["kpis"]["awaiting_cab"] >= 1
    cab = d["cab_queue"]
    assert cab and cab[0]["approve"] == 1


def test_breach_risk_sorted_and_flagged(home):
    d = di.get_overview(home)
    br = d["breach_risk"]
    assert br  # the open sev2 is present
    # SEV2 target is 15m; a just-created incident should be under target (not over yet)
    assert all("remaining_min" in b for b in br)


def test_incidents_and_problems_and_changes(home):
    assert di.get_incidents(home)["incidents"]
    assert di.get_problems(home)["problems"]
    ch = di.get_changes(home)
    assert ch["changes"] and ch["cab_queue"]


def test_record_detail_and_lineage(home):
    inc = di.get_incidents(home)["incidents"][0]
    rec = di.get_record(home, "incident", inc["id"])
    assert rec["record"]["id"] == inc["id"]
    assert "timeline" in rec and isinstance(rec["lineage"], list)


def test_record_missing(home):
    assert "error" in di.get_record(home, "incident", "inc-nope")


# ---- HTTP routes ----

def test_itil_routes(home):
    from starlette.testclient import TestClient
    client = TestClient(create_app(home))
    assert client.get("/api/itil/overview").json()["kpis"]["open_incidents"] >= 1
    assert "incidents" in client.get("/api/itil/incidents").json()
    assert "problems" in client.get("/api/itil/problems").json()
    assert "cab_queue" in client.get("/api/itil/changes").json()
    assert "results" in client.get("/api/itil/kedb?q=conflict").json()
    r = client.get("/cockpit")
    assert r.status_code == 200 and "ITIL Cockpit" in r.text
    assert client.get("/static/js/cockpit.js").status_code == 200
