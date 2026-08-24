from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]


def test_schedule_page_is_read_only_and_discoverable(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: False))
    response = client.get("/control-plane/schedule")
    assert response.status_code == 200
    assert "Roadmap, Gantt, and Flow" in response.text
    assert 'id="schedule-table-rows"' in response.text
    assert 'id="schedule-dependency-rows"' in response.text
    assert client.post("/control-plane/schedule").status_code == 405


def test_schedule_api_fails_closed_without_typed_provider(tmp_path: Path) -> None:
    app = create_app(tmp_path, control_plane_authorizer=lambda *_: True)
    response = TestClient(app).get(
        "/api/v1/schedule/projection?role=project-manager&scope=estate&window=latest&baseline=none&service=all&lens=roadmap&timezone=UTC",
        headers={"Authorization": "Bearer legacy"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "SCHEDULE_UNAVAILABLE"


def test_schedule_surface_preserves_required_lenses_and_accessible_alternatives() -> None:
    html = (ROOT / "src/skdashboard/static/schedule.html").read_text(encoding="utf-8")
    js = (ROOT / "src/skdashboard/static/js/schedule.js").read_text(encoding="utf-8")
    css = (ROOT / "src/skdashboard/static/css/schedule.css").read_text(encoding="utf-8")
    for marker in (
        "Roadmap",
        "Gantt",
        "Flow",
        "Accessible schedule table",
        "Dependency list",
        "Export snapshot",
        "No-write boundary",
        'id="schedule-detail"',
    ):
        assert marker in html
    for marker in (
        "planned_start",
        "planned_target",
        "baseline_start",
        "baseline_target",
        "actual_start",
        "actual_finish",
        "cycle_analysis",
        "critical_path",
        "conflict_state",
        "projection_hash",
        "selected_item",
        'context.lens === "roadmap"',
        'context.lens !== "gantt"',
        'context.lens === "flow"',
        "lastTrigger.focus()",
    ):
        assert marker in js
    assert "@media(max-width:760px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css
    assert "fetch(" not in js
    assert "/api/v1/schedule/projection" in js


def test_schedule_ui_has_no_mutation_or_individual_ranking() -> None:
    text = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "src/skdashboard/static/schedule.html",
            "src/skdashboard/static/js/schedule.js",
        )
    ).lower()
    for forbidden in (
        "owner ranking",
        "person ranking",
        "productivity score",
        "tokens_by_person",
        "commits_by_person",
        "joules_by_person",
        "authorize reschedule",
    ):
        assert forbidden not in text
    assert "cannot create scenarios" in text
    assert "mutate owner records" in text
