"""Operator coverage, provenance, filtering, and impact views for CMDB."""

from __future__ import annotations

from pathlib import Path

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_reconcile import write_run_artifact
from skcoord.discovery import DISCOVERED_TAG, DiscoveredCI
from skcoord.itil import ITILManager
from starlette.testclient import TestClient

from skdashboard.dashboard import create_app
from skdashboard.dashboard_cmdb import apply, get_ci, get_overview, search


def test_overview_reports_coverage_failures_and_reconcile_history(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci(
        "Inventory API",
        "service",
        node="chiap04",
        attributes={
            "observed_at": "2026-08-22T11:59:00+00:00",
            "source_authority": "network:chiap04",
        },
        tags=[DISCOVERED_TAG],
    )
    write_run_artifact(
        tmp_path,
        {
            "scan_id": "scan-coverage",
            "ended_at": "2026-08-22T12:00:00+00:00",
            "applied": True,
            "duration_seconds": 3.5,
            "completeness": {
                "complete": True,
                "collectors_expected": 4,
                "collectors_complete": 3,
                "collectors_unavailable": 1,
            },
            "collector_health": {
                "targets": [
                    {
                        "host": "chiap04",
                        "provenance": ["fleet:node:chiap04"],
                        "expected_collectors": 4,
                        "completed_collectors": 3,
                        "complete": False,
                        "coverage": [{"collector": "systemd", "status": "complete"}],
                        "failures": ["ports:transport_unavailable"],
                        "findings": 8,
                    }
                ]
            },
            "drift": {"count": 2, "by_severity": {"medium": 2}},
        },
    )

    result = get_overview(tmp_path)

    assert result["total"] == 1
    assert result["coverage"]["coverage_percent"] == 75.0
    assert result["coverage"]["nodes"][0]["node"] == "chiap04"
    assert result["evidence_health"]["unreachable"] == 1
    assert result["last_successful_reconciliation"] == "2026-08-22T12:00:00+00:00"
    assert result["reconciliation_history"][0]["drift"] == 2


def test_detail_exposes_provenance_endpoints_history_relations_and_itil(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    host = mgr.create_ci("chiap04", "host")
    service = mgr.create_ci(
        "Legal API",
        "service",
        owner="platform",
        node="chiap04",
        attributes={
            "port": 7778,
            "observed_at": "2026-08-22T12:00:00+00:00",
            "source_authority": "network:chiap04",
            "scan_id": "scan-detail",
        },
        tags=[DISCOVERED_TAG],
    )
    mgr.add_relationship(service.id, "test", "runs_on", host.id, authority="observed")
    mgr.set_status(service.id, "health", "degraded", note="probe failed")
    incident = ITILManager(tmp_path).create_incident(
        "Legal API latency",
        affected_services=[service.id],
        managed_by="test",
    )

    result = get_ci(tmp_path, service.id)

    assert result["owner"] == "platform"
    assert result["endpoints"] == {"port": 7778}
    assert result["provenance"]["source"] == "network:chiap04"
    assert result["last_seen"] == "2026-08-22T12:00:00+00:00"
    assert result["health_history"][0]["status"] == "degraded"
    assert result["relationship_groups"]["runs_on"][0]["target"] == host.id
    assert result["linked_itil"][0]["id"] == incident.id


def test_search_combines_supported_operator_filters(tmp_path: Path) -> None:
    mgr = CMDBManager(tmp_path)
    mgr.create_ci(
        "Stale Worker",
        "service",
        owner="operations",
        node="noroc2027",
        attributes={
            "observed_at": "2020-01-01T00:00:00+00:00",
            "source_authority": "network:noroc2027",
        },
        tags=[DISCOVERED_TAG, "worker"],
    )
    mgr.create_ci("Unrelated Host", "host", owner="operations")

    result = search(
        tmp_path,
        "",
        ci_type="service",
        node="noroc2027",
        status="operational",
        owner="operations",
        tag="worker",
        staleness="stale",
        source="network:noroc2027",
    )

    assert result["total"] == 1
    assert result["items"][0]["name"] == "Stale Worker"
    assert result["items"][0]["staleness"] == "stale"


def test_discovery_e2e_updates_drift_impact_and_deduplicates_rescan(
    tmp_path: Path, monkeypatch
) -> None:
    port = {"value": 7778}

    def scan_fixture(*_args, **_kwargs):
        return [
            DiscoveredCI("host", "chiap04", "fleet:node", tags=(DISCOVERED_TAG,)),
            DiscoveredCI(
                "service",
                "Matter API",
                "systemd",
                observed=True,
                node="chiap04",
                attributes={"port": port["value"]},
                tags=(DISCOVERED_TAG,),
                relationships=(("runs_on", "ci-host-chiap04"),),
                observed_at="2026-08-22T12:00:00+00:00",
                scan_id="e2e-scan",
                authority="network:chiap04",
            ),
        ]

    monkeypatch.setattr("skcoord.discovery.scan", scan_fixture)

    first = apply(tmp_path)
    assert first["counts"]["created"] == 2
    service = CMDBManager(tmp_path).get_ci("ci-service-matter-api")
    assert service is not None
    assert get_ci(tmp_path, service.id)["relationship_groups"]["runs_on"]

    port["value"] = 7780
    second = apply(tmp_path)
    assert second["counts"]["updated"] == 1
    assert get_ci(tmp_path, service.id)["ci"]["attributes"]["port"] == 7780

    third = apply(tmp_path)
    assert third["counts"]["created"] == 0
    assert third["counts"]["updated"] == 0


def test_plan_api_reports_preview_authorization_and_execution_state(tmp_path: Path) -> None:
    result = TestClient(create_app(tmp_path)).get("/api/cmdb/plan").json()

    assert result["preview"] is True
    assert result["execution_state"] == "not_executed"
    assert result["authorization"]["evaluated"] is True
    assert result["authorization"]["authorized"] is True


def test_plan_stays_readable_but_apply_fails_closed_without_capability(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SKAI_AUTHZ", raising=False)
    monkeypatch.setenv("SKAI_QUEUE_TOKEN", "expected-token")
    client = TestClient(create_app(tmp_path))

    preview = client.get("/api/cmdb/plan").json()
    denied = client.post("/api/cmdb/apply")

    assert preview["preview"] is True
    assert preview["authorization"]["authorized"] is False
    assert denied.status_code == 403
    assert CMDBManager(tmp_path).list_cis() == []


def test_cmdb_page_ships_operator_filters_and_action_state(tmp_path: Path) -> None:
    page = TestClient(create_app(tmp_path)).get("/cmdb")

    assert page.status_code == 200
    for control in ('name="type"', 'name="node"', 'name="staleness"', 'name="source"'):
        assert control in page.text
    assert 'id="cmdb-coverage"' in page.text
    assert 'id="cmdb-action-state"' in page.text
