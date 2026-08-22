"""CMDB plan/apply/status dashboard integration."""

from __future__ import annotations

from pathlib import Path

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_reconcile import write_run_artifact
from skcoord.discovery import DISCOVERED_TAG, DiscoveredCI
from starlette.testclient import TestClient

from skdashboard.dashboard import create_app
from skdashboard.dashboard_cmdb import apply, plan, status


def _fixture_scan(monkeypatch) -> None:
    item = DiscoveredCI(
        "host",
        "chiap04",
        "fleet:node",
        tags=(DISCOVERED_TAG,),
        authority="declared",
    )
    monkeypatch.setattr("skcoord.discovery.scan", lambda *_args, **_kwargs: [item])


def test_plan_is_write_free_and_apply_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _fixture_scan(monkeypatch)

    preview = plan(tmp_path)
    assert preview["applied"] is False
    assert preview["counts"]["created"] == 1
    assert CMDBManager(tmp_path).list_cis() == []

    assert apply(tmp_path)["counts"]["created"] == 1
    second = apply(tmp_path)
    assert second["counts"]["created"] == 0
    assert second["counts"]["updated"] == 0


def test_status_uses_only_checksum_verified_artifacts(tmp_path: Path) -> None:
    write_run_artifact(
        tmp_path,
        {
            "scan_id": "verified",
            "ended_at": "2026-08-21T12:00:00+00:00",
            "completeness": {"complete": True},
            "drift": {"count": 0, "by_severity": {}},
        },
    )
    bad = tmp_path / "cmdb" / "reconcile-runs" / "tampered.json"
    bad.write_text('{"scan_id":"tampered"}\n')
    bad.with_suffix(".sha256").write_text("0" * 64 + "  tampered.json\n")

    result = status(tmp_path)

    assert result["latest_scan_id"] == "verified"
    assert result["inventory"]["total"] == 0


def test_supported_cmdb_api_routes_are_registered(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))

    assert client.get("/api/cmdb/status").status_code == 200
    assert client.get("/api/cmdb/plan").status_code == 200
    assert client.get("/api/cmdb/drift").status_code == 200
    assert client.post("/api/cmdb/apply").status_code == 200
