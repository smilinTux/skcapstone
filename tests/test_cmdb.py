"""Tests for the Phase 6 CMDB / asset management (cmdb + dashboard_cmdb)."""

from __future__ import annotations

import pytest

from skcapstone import dashboard_cmdb as dc
from skcapstone.cmdb import CMDBManager, make_ci_id


@pytest.fixture
def home(tmp_path):
    return tmp_path


def test_create_and_get_ci(home):
    mgr = CMDBManager(home)
    ci = mgr.create_ci("skgateway", "service", node="noroc2027", attributes={"port": 18780})
    assert ci.id == make_ci_id("service", "skgateway")
    got = mgr.get_ci(ci.id)
    assert got.name == "skgateway" and got.attributes["port"] == 18780
    assert got.status == "operational"


def test_create_is_idempotent(home):
    mgr = CMDBManager(home)
    a = mgr.create_ci("skmem-pg", "datastore")
    b = mgr.create_ci("skmem-pg", "datastore")
    assert a.id == b.id
    assert len(mgr.list_cis()) == 1


def test_status_and_relationships_fold(home):
    mgr = CMDBManager(home)
    host = mgr.create_ci("noroc2027", "host")
    svc = mgr.create_ci("skchat", "service")
    mgr.set_status(svc.id, "opus", "down", note="daemon crash")
    mgr.add_relationship(svc.id, "opus", "runs_on", host.id)
    folded = mgr.get_ci(svc.id)
    assert folded.status == "down"
    assert any(r.rel_type == "runs_on" and r.target == host.id for r in folded.relationships)
    mgr.remove_relationship(svc.id, "opus", "runs_on", host.id)
    assert not mgr.get_ci(svc.id).relationships


def test_impact_analysis(home):
    mgr = CMDBManager(home)
    host = mgr.create_ci("noroc2027", "host")
    svc = mgr.create_ci("skchat", "service")
    mgr.add_relationship(svc.id, "opus", "runs_on", host.id)
    impact = mgr.impact_analysis(host.id)
    assert any(d["id"] == svc.id for d in impact["dependents"])


def test_impact_links_incidents(home):
    from skcapstone.itil import ITILManager

    mgr = CMDBManager(home)
    svc = mgr.create_ci("skchat", "service")
    itil = ITILManager(home)
    itil.create_incident(
        title="skchat down", severity="sev2", created_by="opus", affected_services=["skchat"]
    )
    impact = mgr.impact_analysis(svc.id)
    assert impact["open_incidents"] and impact["open_incidents"][0]["severity"] == "sev2"


def test_seed_from_inventory(home):
    registry = home / "registry"
    registry.mkdir()
    (registry / "skmem-pg.json").write_text('{"name":"skmem-pg"}')
    mgr = CMDBManager(home)
    res = mgr.seed_from_inventory()
    assert res["schema"] == "skcoord.cmdb.compat-seed/v1"
    assert res["deprecated"] is True
    assert res["cis"] == 1
    svc = mgr.find_for_service("skmem-pg")
    assert svc and svc.status == "operational"
    assert svc.attributes["source_authority"] == "declared"


def test_dashboard_cmdb_overview_and_detail(home):
    registry = home / "registry"
    registry.mkdir()
    (registry / "skmem-pg.json").write_text('{"name":"skmem-pg"}')
    # Populate the isolated fixture through the compatibility importer.  The
    # dashboard ``seed`` route now performs real local discovery and must not
    # scan the CI runner from a unit test.
    CMDBManager(home).seed_from_inventory()
    ov = dc.get_overview(home)
    assert ov["total"] == 1 and ov["types"]
    sid = make_ci_id("service", "skmem-pg")
    detail = dc.get_ci(home, sid)
    assert detail["ci"]["id"] == sid
    assert "relationships" in detail and "dependents" in detail


def test_dashboard_cmdb_routes(home, monkeypatch):
    from starlette.testclient import TestClient

    from skcapstone.dashboard import create_app

    registry = home / "registry"
    registry.mkdir()
    (registry / "skmem-pg.json").write_text('{"name":"skmem-pg"}')

    def _compat_reconcile(target_home, *, apply):
        assert apply is True
        return CMDBManager(target_home).seed_from_inventory()

    monkeypatch.setattr(dc, "_local_reconcile", _compat_reconcile)
    client = TestClient(create_app(home))
    response = client.post("/api/cmdb/seed").json()
    assert response["cis"] == 1
    assert response["deprecated"] is True
    assert "types" in client.get("/api/cmdb/overview").json()
    r = client.get("/cmdb")
    assert r.status_code == 200 and "CMDB" in r.text


def test_seed_never_invents_assets(home):
    # An empty home has no declared sources: the reconciler must not fabricate any.
    mgr = CMDBManager(home)
    res = mgr.seed_from_inventory()
    assert res["cis"] == 0 and res["touched"] == 0
