from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_reconcile import write_run_artifact
from starlette.testclient import TestClient

from skdashboard.control_plane_adapters import aggregate_reader
from skdashboard.dashboard import create_app
from skdashboard.dashboard_architecture import get_architecture_projection

ROOT = Path(__file__).parents[1]
NOW = datetime.now(timezone.utc)
QUERY = {
    "role": "architect",
    "scope": "estate",
    "window": "latest",
    "baseline": "none",
    "service": "all",
    "environment": "all",
}


def _fixture(tmp_path: Path) -> None:
    manager = CMDBManager(tmp_path)
    host = manager.create_ci(
        "chiap04",
        "host",
        owner="infrastructure",
        attributes={
            "environment": "production",
            "observed_at": NOW.isoformat(),
            "source_authority": "fleet:chiap04",
            "scan_id": "scan-23",
        },
    )
    api = manager.create_ci(
        "Control Plane API",
        "service",
        owner="platform",
        node="chiap04",
        attributes={
            "environment": "production",
            "observed_at": NOW.isoformat(),
            "source_authority": "systemd:chiap04",
            "scan_id": "scan-23",
        },
    )
    worker = manager.create_ci(
        "Legacy Worker",
        "service",
        node="chiap04",
        attributes={
            "environment": "production",
            "observed_at": NOW.isoformat(),
            "source_authority": "systemd:chiap04",
            "scan_id": "scan-23",
        },
        tags=["unsupported"],
    )
    manager.add_relationship(api.id, "test", "runs_on", host.id, authority="observed")
    manager.add_relationship(worker.id, "test", "depends_on", api.id, authority="declared")
    manager.set_status(worker.id, "test", "degraded")
    write_run_artifact(
        tmp_path,
        {
            "scan_id": "scan-23",
            "ended_at": NOW.isoformat(),
            "applied": True,
            "completeness": {
                "complete": True,
                "collectors_expected": 3,
                "collectors_complete": 3,
                "collectors_unavailable": 0,
            },
            "collector_health": {"targets": []},
            "drift": {"count": 2},
        },
    )


def test_architecture_projection_preserves_unknowns_topology_and_approved_aggregates(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    readers = {
        "skcapstone.service_release": aggregate_reader(
            {"services": 2, "releases": 3}, observed_at=NOW.isoformat()
        ),
        "skperf.aggregate": aggregate_reader(
            {"regressions": 1, "capacity_pressure": 0.72},
            expected=4,
            reporting=4,
            observed_at=NOW.isoformat(),
            watermark_data="approved-fixture",
        ),
    }
    projection = get_architecture_projection(
        tmp_path,
        QUERY,
        aggregate_readers=readers,
        now=NOW,
    )
    metrics = {item["metric_id"]: item for item in projection["metrics"]}
    nodes = {item["name"]: item for item in projection["topology"]["nodes"]}

    for metric_id in (
        "dora.deployment_frequency",
        "dora.lead_time_for_changes",
        "dora.change_failure_rate",
        "dora.failed_deployment_recovery_time",
        "dora.deployment_rework_rate",
        "engineering.release_quality",
        "architecture.adr_freshness",
        "architecture.technical_debt_exposure",
    ):
        assert metrics[metric_id]["truth_state"] == "unknown"
        assert metrics[metric_id]["value"] is None
    assert metrics["skperf.regressions"]["value"] == 1
    assert metrics["architecture.capacity_pressure"]["value"] == 0.72
    assert metrics["skperf.regressions"]["baseline"] is None
    assert "approved" in metrics["skperf.regressions"]["definition"].lower()
    assert metrics["cmdb.configuration_drift"]["value"] == 2
    assert metrics["cmdb.owner_coverage"]["numerator"] == 2
    assert metrics["cmdb.owner_coverage"]["denominator"] == 3
    assert nodes["chiap04"]["blast_radius"]["dependent_count"] == 2
    assert set(nodes["chiap04"]["blast_radius"]["impacted_service_ids"]) == {
        "ci-service-control-plane-api",
        "ci-service-legacy-worker",
    }
    assert nodes["Legacy Worker"]["unsupported"] is True
    assert any(item["ci_id"] == "ci-service-legacy-worker" for item in projection["exceptions"])
    assert "approved-fixture" not in str(projection)
    serialized = str(projection).lower()
    assert "raw_target" not in serialized
    assert "protected_path" not in serialized
    assert projection["individual_ranking_prohibited"] is True


def test_empty_architecture_source_is_unknown_not_zero(tmp_path: Path) -> None:
    projection = get_architecture_projection(tmp_path, QUERY, now=NOW)
    assert projection["truth_state"] == "unknown"
    assert projection["topology"]["total_cis"] == 0
    assert all(item["value"] is None for item in projection["metrics"])


def test_architecture_page_is_read_only_and_table_first(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: False))
    response = client.get("/control-plane/architecture")
    assert response.status_code == 200
    assert "DORA, topology, capacity, and drift" in response.text
    assert 'id="architecture-metric-rows"' in response.text
    assert 'id="architecture-exception-rows"' in response.text
    assert 'id="architecture-node-rows"' in response.text
    assert 'id="architecture-edge-rows"' in response.text
    assert client.post("/control-plane/architecture").status_code == 405


def test_architecture_api_fails_closed_without_typed_provider(tmp_path: Path) -> None:
    response = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: True)).get(
        "/api/v1/architecture/projection?role=architect&scope=estate&window=latest&baseline=none&service=all&environment=all",
        headers={"Authorization": "Bearer legacy"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "ARCHITECTURE_UNAVAILABLE"


def test_architecture_surface_has_no_write_or_ranking_controls() -> None:
    html = (ROOT / "src/skdashboard/static/architecture.html").read_text()
    js = (ROOT / "src/skdashboard/static/js/architecture.js").read_text()
    css = (ROOT / "src/skdashboard/static/css/architecture.css").read_text()
    for marker in (
        "DORA and architecture measurements",
        "Architecture exceptions",
        "CMDB topology and blast radius",
        "Approved aggregate and no-write boundary",
        'id="architecture-detail"',
    ):
        assert marker in html
    assert "/api/v1/architecture/projection" in js
    assert "fetch(" not in js
    assert "POST" not in js
    assert "mutate" not in js
    assert "@media(max-width:760px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
