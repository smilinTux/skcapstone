from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from starlette.testclient import TestClient

from skdashboard.dashboard import create_app

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
ARCHITECTURE_FILES = [
    ROOT / "docs" / "architecture" / "ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING.md",
    ROOT / "docs" / "planning" / "SK-CONTROL-PLANE-BREADTH-FIRST-SPRINTS.md",
    ROOT / "docs" / "wireframes" / "control-plane-estate-pulse.html",
]
CONTRACT_FILES = sorted(CONTRACTS.glob("*.json"))
REVIEW_MANIFEST = ROOT / "docs" / "review" / "SKCP-00-CANDIDATE-MANIFEST.json"


def _walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_control_plane_contract_json_is_parseable_and_local_refs_exist() -> None:
    assert {path.name for path in CONTRACT_FILES} == {
        "control-plane-action-preview.schema.json",
        "control-plane-insight.schema.json",
        "control-plane-metric-result.schema.json",
        "control-plane-recommendation.schema.json",
        "control-plane-report-snapshot.schema.json",
        "openapi.control-plane.v1.json",
    }

    for path in CONTRACT_FILES:
        document = json.loads(path.read_text(encoding="utf-8"))
        for node in _walk(document):
            ref = node.get("$ref")
            if not ref or ref.startswith(("#", "https://")):
                continue
            relative_path = ref.split("#", 1)[0]
            assert (path.parent / relative_path).is_file(), f"missing $ref from {path}: {ref}"


def test_openapi_freezes_required_read_ai_and_authorization_boundaries() -> None:
    document = json.loads(
        (CONTRACTS / "openapi.control-plane.v1.json").read_text(encoding="utf-8")
    )
    assert document["openapi"] == "3.1.0"
    paths = document["paths"]
    assert {
        "/health",
        "/overview",
        "/board/summary",
        "/fleet/summary",
        "/economy/summary",
        "/reports/{snapshot_id}",
        "/insights/query",
        "/actions/preview",
        "/action-previews/{preview_id}/authorize",
        "/events",
    }.issubset(paths)
    assert paths["/events"]["get"]["responses"]["200"]["content"]["text/event-stream"]
    assert "cannot invoke the command API" in paths["/insights/query"]["post"][
        "description"
    ]
    assert "revalidates policy" in paths["/action-previews/{preview_id}/authorize"][
        "post"
    ]["description"]


def test_metric_truth_and_ai_recommendation_contracts_are_explicit() -> None:
    metric = json.loads(
        (CONTRACTS / "control-plane-metric-result.schema.json").read_text(encoding="utf-8")
    )
    truth_states = metric["properties"]["truth_state"]["enum"]
    assert truth_states == [
        "current",
        "stale",
        "partial",
        "unavailable",
        "unknown",
        "not_applicable",
    ]
    assert set(metric["properties"]["measurement_kind"]["enum"]) == {
        "measured",
        "derived",
        "estimated",
        "forecast",
    }

    insight = json.loads(
        (CONTRACTS / "control-plane-insight.schema.json").read_text(encoding="utf-8")
    )
    assert "recommendations" in insight["required"]
    recommendation = json.loads(
        (CONTRACTS / "control-plane-recommendation.schema.json").read_text(
            encoding="utf-8"
        )
    )
    required = set(recommendation["required"])
    assert {
        "best_practice_refs",
        "expected_impact",
        "confidence",
        "risks",
        "counter_indicators",
        "alternatives",
        "preconditions",
        "next_step",
    }.issubset(required)


def test_action_preview_requires_exact_review_and_rollback_evidence() -> None:
    preview = json.loads(
        (CONTRACTS / "control-plane-action-preview.schema.json").read_text(encoding="utf-8")
    )
    required = set(preview["required"])
    assert {
        "preview_hash",
        "owner_service",
        "owner_operation",
        "expected_version",
        "blast_radius",
        "risk",
        "verification_plan",
        "rollback_plan",
        "required_scope",
        "required_approvals",
        "policy_decision_ref",
        "expires_at",
    }.issubset(required)


def test_wireframe_is_self_contained_accessible_and_clearly_synthetic() -> None:
    wireframe = ARCHITECTURE_FILES[-1].read_text(encoding="utf-8")
    assert "Interactive wireframe" in wireframe
    assert "synthetic data" in wireframe
    assert "Estate intelligence brief" in wireframe
    assert "Decision queue" in wireframe
    assert "Estate pulse" in wireframe
    assert "Recommended next moves" in wireframe
    assert "Authorization preview" in wireframe
    assert "Approve and queue exact preview" in wireframe
    assert 'aria-label="Control plane workspaces"' in wireframe
    assert 'aria-live="polite"' in wireframe
    assert "prefers-reduced-motion" in wireframe
    assert not re.search(r'(?:src|href)="https?://', wireframe)


def test_new_control_plane_artifacts_use_ascii_dashes_only() -> None:
    for path in [*ARCHITECTURE_FILES, *CONTRACT_FILES, REVIEW_MANIFEST, Path(__file__)]:
        text = path.read_text(encoding="utf-8")
        assert "\u2013" not in text, f"en dash in {path}"
        assert "\u2014" not in text, f"em dash in {path}"


def test_human_review_manifest_pins_exact_artifacts_and_keeps_gate_closed() -> None:
    manifest = json.loads(REVIEW_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["status"] == "proposed_for_human_review"
    assert manifest["implementation_authorized"] is False
    assert manifest["human_review_card_id"] == "9508b8fd"
    assert set(manifest["sprint_containers"]) == {"0", "1", "2", "3", "4", "5"}
    assert len(manifest["leaf_cards"]) == 22

    for artifact in manifest["artifacts"]:
        path = ROOT / artifact["path"]
        assert path.is_file()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest == artifact["sha256"], artifact["path"]


def test_unknown_control_plane_routes_fail_closed_without_fallback(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path))
    for method in ("GET", "POST"):
        response = client.request(method, "/api/v1/unsupported-route")
        assert response.status_code == 404
        assert "SKDashboard" not in response.text
        assert "active_tasks" not in response.text
