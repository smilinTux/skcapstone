from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]


def test_portfolio_route_is_read_only_and_uses_the_canonical_wireframe_path(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: False))
    response = client.get("/control-plane/portfolio")
    assert response.status_code == 200
    assert "Portfolio, projects, and Agile flow" in response.text
    assert 'id="project-table"' in response.text
    assert client.post("/control-plane/portfolio").status_code == 405


def test_project_workspace_keeps_every_measurement_boundary_explicit() -> None:
    html = (ROOT / "src/skdashboard/static/projects.html").read_text(encoding="utf-8")
    js = (ROOT / "src/skdashboard/static/js/projects.js").read_text(encoding="utf-8")
    css = (ROOT / "src/skdashboard/static/css/projects.css").read_text(encoding="utf-8")
    overview = (ROOT / "src/skdashboard/static/js/overview.js").read_text(encoding="utf-8")

    for marker in (
        "Measurement and traceability",
        "Sample and population",
        "Window",
        "Exclusions",
        'id="project-evidence"',
        "Velocity is local planning context only",
        "Never compare teams or rank people",
        "separate legacy source view",
    ):
        assert marker in html
    for signal in (
        "Owner record traceability",
        "Objectives",
        "Explicit benefit records",
        "Current value",
        "Unrealized value",
        "Investment",
        "Cost of delay",
        "Decision latency",
        "Current CardStore WIP context",
        "Throughput",
        "Open record age P50, P85, P95",
        "Cycle time P50, P85, P95",
        "Blocked time",
        "Flow efficiency",
        "Churn",
        "Rollover",
        "Sprint goal result",
        "Stale unresolved record-activity paths",
        "Orphaned dependency paths",
        "Conflicted dependency paths",
        "Human-gated paths",
        "Milestones",
        "Project and portfolio risk",
        "Forecast inputs",
    ):
        assert signal in js
    for boundary in (
        "Current done stock is not throughput",
        "Current blocked stock is not duration",
        "Adapter coverage is not review coverage",
        "No forecast range is produced",
        "not a work-item sample",
        "No person or team ranking",
    ):
        assert boundary in html + js
    assert "getJSON(apiUrl(context))" in js
    assert "responseMatches(response, context)" in js
    assert "epoch !== loadEpoch" in js
    assert 'url.pathname = "/control-plane/portfolio"' in js
    assert "saved_view" in js
    assert "fetch(" not in js
    assert "/api/kanban" not in js
    assert "get_kanban" not in js
    assert "prefers-reduced-motion:reduce" in css
    assert "@media(max-width:560px)" in css
    assert "Portfolio project workspace" in overview
    assert "safeSearch({ ...currentContext" in overview
    for released_field in (
        "authorized_ids",
        "folded",
        "emitted_records",
        "visible_edges",
        "explicit_milestones",
        "visible_dependency_count",
    ):
        assert released_field in js
    for nonexistent_field in (
        "source_record_count",
        "dependency_summary",
        "open_record_age",
        "record.dependency_count",
    ):
        assert nonexistent_field not in js
    assert "project.truncated || project.classification_complete === false" in js


def test_project_workspace_has_no_individual_productivity_aggregation() -> None:
    js = (ROOT / "src/skdashboard/static/js/projects.js").read_text(encoding="utf-8")
    forbidden = (
        "groupByOwner",
        "ownerRanking",
        "agentRanking",
        "commits_by_person",
        "tokens_by_person",
        "joules_by_person",
    )
    assert not any(marker in js for marker in forbidden)
    assert "Cards, tokens, cost, commits, and Joules are not investment proxies" in js
