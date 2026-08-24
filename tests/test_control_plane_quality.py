from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from skdashboard.control_plane_adapters import SPECS
from skdashboard.control_plane_metric_registry import DEFINITIONS, TRUTH_STATES
from skdashboard.control_plane_quality import project_data_quality
from skdashboard.dashboard import create_app
from skdashboard.dashboard_overview import get_overview_home

ROOT = Path(__file__).parents[1]
READ_HEADERS = {
    "Authorization": "Bearer quality-read",
    "Origin": "http://10.0.0.139:7778",
}


def _authorizer(bearer: str, capability: str, _target: str) -> bool:
    return bearer == "quality-read" and capability == "skdashboard.read"


def _observations() -> list[dict]:
    states = tuple(sorted(TRUTH_STATES))
    items = []
    for index, spec in enumerate(SPECS):
        state = states[index % len(states)]
        failed = state in {"unavailable", "unreachable", "unknown"}
        items.append(
            {
                "adapter_id": spec.adapter_id,
                "adapter_version": "1.0.0",
                "owner": spec.owner,
                "truth_state": state,
                "observed_at": None if failed else "2026-08-24T12:00:00Z",
                "watermark": {
                    "source": spec.adapter_id,
                    "value": None if failed else f"sha256:{index:064x}",
                },
                "coverage": {
                    "expected": None if failed else 4,
                    "reporting": None if failed else (3 if state == "partial" else 4),
                },
                "errors": (
                    [{"code": f"SOURCE_{state.upper()}", "message": "Safe detail"}]
                    if state in {"partial", "unavailable", "unreachable"}
                    else []
                ),
            }
        )
    return items


def test_projection_keeps_every_truth_state_coverage_and_metric_registry_visible() -> None:
    quality = project_data_quality(_observations())
    assert set(quality["state_counts"]) == set(TRUTH_STATES)
    assert all(quality["state_counts"][state] > 0 for state in TRUTH_STATES)
    assert quality["source_count"] == len(SPECS)
    assert quality["coverage"] == {
        "reporting": sum(
            item["truth_state"] not in {"unavailable", "unreachable", "unknown"}
            for item in _observations()
        ),
        "expected": len(SPECS),
        "percent": 62.5,
        "population": "declared_sources",
    }
    assert quality["issue_count"] == sum(
        quality["state_counts"][state]
        for state in TRUTH_STATES - {"current", "not_applicable"}
    )
    assert quality["metric_registry"]["definition_count"] == len(DEFINITIONS)
    assert quality["metric_registry"]["registry_hash"].startswith("sha256:")
    assert quality["actions"] == {
        "mode": "preview_only",
        "dispatch_authorized": False,
    }


def test_failed_source_never_becomes_zero_coverage_or_empty_green() -> None:
    quality = project_data_quality(_observations())
    failed = next(
        issue for issue in quality["issues"] if issue["truth_state"] == "unavailable"
    )
    assert failed["coverage"] == {"reporting": None, "expected": None, "percent": None}
    assert failed["watermark"]["value"] is None
    assert failed["last_observation"] is None
    assert failed["safe_provenance"]
    assert failed["safe_next_step"] == {
        "kind": "refresh_preview",
        "label": "Preview refresh",
        "preview_only": True,
        "dispatch_authorized": False,
    }
    assert quality["truth_state"] != "current"


def test_stale_and_partial_sources_receive_truthful_fallback_provenance() -> None:
    observations = _observations()
    observations = [{**item, "errors": []} for item in observations]
    quality = project_data_quality(observations)
    stale = next(issue for issue in quality["issues"] if issue["truth_state"] == "stale")
    partial = next(issue for issue in quality["issues"] if issue["truth_state"] == "partial")
    assert stale["safe_provenance"] == [{
        "code": "EVIDENCE_STALE",
        "message": "The last observation exceeded its freshness TTL",
    }]
    assert partial["safe_provenance"] == [{
        "code": "COVERAGE_PARTIAL",
        "message": "Only part of the declared population reported",
    }]


def test_projection_rejects_missing_duplicate_and_unknown_source_truth() -> None:
    valid = _observations()
    for broken in (
        valid[:-1],
        [*valid[:-1], valid[0]],
        [{**item, "truth_state": "healthy"} if index == 0 else item for index, item in enumerate(valid)],
    ):
        with pytest.raises(ValueError, match="one valid observation per adapter"):
            project_data_quality(broken)


def test_read_only_quality_api_exposes_projection_without_mutation(tmp_path: Path) -> None:
    with patch(
        "skdashboard.control_plane_adapters.default_readers", return_value={}
    ), patch(
        "skdashboard.control_plane_adapters.project_estate", return_value=_observations()
    ), patch(
        "skcoord.coordination.Board.claim", side_effect=AssertionError("mutation"), create=True
    ):
        response = TestClient(
            create_app(tmp_path, control_plane_authorizer=_authorizer)
        ).get("/api/v1/overview", headers=READ_HEADERS)
    assert response.status_code == 200
    assert response.headers["etag"]
    body = response.json()
    assert body["freshness"]["truth_state"] != "current"
    quality = next(item for item in body["items"] if item.get("projection_type") == "data_quality")
    assert quality["actions"]["dispatch_authorized"] is False
    client = TestClient(create_app(tmp_path, control_plane_authorizer=_authorizer))
    assert client.get("/api/v1/data-quality").status_code == 404
    assert client.post("/api/v1/overview", headers=READ_HEADERS).status_code == 405


def test_legacy_overview_marks_failed_sources_unavailable_instead_of_zero(
    tmp_path: Path,
) -> None:
    from skcoord.card import LANE_ORDER

    grid = {
        lane: {column: [] for column in ("backlog", "ready", "doing", "review", "done")}
        for lane in LANE_ORDER
    }
    with patch("skcoord.card.KanbanBoard.grid", return_value=grid), patch(
        "skcoord.card.KanbanBoard.wip_report", return_value={}
    ), patch(
        "skdashboard.dashboard_itil.get_overview", return_value={"error": "offline"}
    ), patch(
        "skdashboard.dashboard_cmdb.get_overview", side_effect=ConnectionError
    ):
        overview = get_overview_home(tmp_path)
    assert overview["itil"]["available"] is False
    assert overview["itil"]["kpis"] == {}
    assert overview["cmdb"] == {"available": False, "total": None, "health": {}}


def test_overview_strip_is_accessible_responsive_non_color_and_preview_only() -> None:
    html = (ROOT / "src/skdashboard/static/overview.html").read_text(encoding="utf-8")
    js = (ROOT / "src/skdashboard/static/js/overview.js").read_text(encoding="utf-8")
    css = (ROOT / "src/skdashboard/static/css/overview.css").read_text(encoding="utf-8")
    for marker in (
        'aria-labelledby="quality-heading"',
        'aria-live="polite"',
        '<dialog id="quality-preview"',
        'aria-label="Close refresh preview"',
        "No dispatch",
    ):
        assert marker in html
    for state in TRUTH_STATES:
        assert state in js
    assert "Coverage unavailable" in js
    assert "sources observed" in js
    assert "metric_registry.registry_version" in js
    assert 'itil.available === true' in js
    assert 'cm.available === true' in js
    assert "health unknown" in js
    assert "Preview refresh" not in js
    assert "showModal()" in js
    assert "fetch(" not in js
    assert "@media(max-width:560px)" in css
    assert "focus-visible" in css
    assert "prefers-reduced-motion" in css
