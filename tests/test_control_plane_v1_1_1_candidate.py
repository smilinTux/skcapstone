from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "review"
ORIGINAL = REVIEW / "SKCP-00-CANDIDATE-MANIFEST.json"
V110 = REVIEW / "SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json"
V111 = REVIEW / "SKCP-00-CANDIDATE-MANIFEST-v1.1.1.json"
SNAPSHOT = REVIEW / "SKCP-00-SCHEDULE-CARD-SNAPSHOT-v1.1.1.json"
REQUIREMENTS = REVIEW / "SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md"
ORIGINAL_SHA256 = "88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3"
V110_SHA256 = "6b35f9e77f8f51dde5243bd9ebc5f55adbf65141d344218b165845bd3475a194"
CAPTURED_AT = "2026-08-23T22:35:28Z"
RAW_PROJECTION_SHA256 = "28db70226ac8ab4716cf26b72bed23040970ec23dc06af22156c233142716be6"
SCHEDULE_CARDS = {
    "c3a9c9e9": ["bea13a70", "d0edbff1", "b7ada8b9"],
    "eddaa1fb": ["bea13a70", "d0edbff1", "b7ada8b9", "5ee56779", "c3a9c9e9"],
    "7888e091": [
        "bea13a70",
        "d0edbff1",
        "169028ce",
        "f080f150",
        "efa9bee8",
        "eddaa1fb",
        "4e1130cc",
    ],
    "4e1130cc": [
        "bea13a70",
        "d0edbff1",
        "169028ce",
        "f080f150",
        "efa9bee8",
        "c3a9c9e9",
    ],
}
LEGACY = {"SKCP-01": "d12b8951", "SKCP-02": "94cbf19a", "SKCP-07": "f0c63c2a"}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _external_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        if isinstance(value.get("$ref"), str) and not value["$ref"].startswith("#"):
            refs.append(value["$ref"].split("#", 1)[0])
        for child in value.values():
            refs.extend(_external_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_external_refs(child))
    return refs


def test_v1_1_1_manifest_truthfully_supersedes_exact_prior_packages() -> None:
    manifest = _load(V111)
    assert _sha256(ORIGINAL) == ORIGINAL_SHA256
    assert _sha256(V110) == V110_SHA256
    assert manifest["manifest_version"] == "2.1.0"
    assert manifest["manifest_schema_version"] == "2020-12"
    assert manifest["candidate_package_version"] == "1.1.1"
    assert manifest["status"] == "proposed_for_human_review"
    assert manifest["implementation_authorized"] is False
    assert manifest["captured_at"] == CAPTURED_AT
    assert manifest["supersedes"] == [
        {
            "path": "docs/review/SKCP-00-CANDIDATE-MANIFEST.json",
            "sha256": ORIGINAL_SHA256,
            "disposition": "superseded_but_valid_for_audit",
        },
        {
            "path": "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json",
            "sha256": V110_SHA256,
            "disposition": "superseded_but_valid_for_audit",
        },
    ]


def test_v1_1_1_non_authoritative_defects_are_preserved_exactly() -> None:
    manifest = _load(V111)
    defects = []
    for artifact in manifest["artifacts"]:
        path = ROOT / artifact["path"]
        if not path.is_file():
            defects.append(("missing", artifact["path"]))
        elif _sha256(path) != artifact["sha256"]:
            defects.append(("superseded", artifact["path"]))
    assert defects == [
        ("superseded", "docs/contracts/CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md"),
        ("superseded", "docs/contracts/v1.1.0/control-plane-action-preview.v1.1.0.schema.json"),
        ("superseded", "docs/contracts/v1.1.0/control-plane-insight.v1.1.0.schema.json"),
        ("superseded", "docs/contracts/v1.1.0/control-plane-metric-result.v1.1.0.schema.json"),
        ("superseded", "docs/contracts/v1.1.0/control-plane-recommendation.v1.1.0.schema.json"),
        ("superseded", "docs/contracts/v1.1.0/control-plane-report-snapshot.v1.1.0.schema.json"),
        ("superseded", "docs/contracts/v1.1.0/openapi.control-plane.v1.1.0.json"),
        ("missing", "docs/wireframes/control-plane-estate-pulse-v2.png"),
        ("missing", "docs/wireframes/control-plane-authorization-preview-v2.png"),
        ("superseded", "docs/evidence/SKCP-00F3F-COMPLETION-DEPENDENCY-GATE-2026-08-23.md"),
        ("superseded", "tests/test_control_plane_superseding_candidate.py"),
        ("superseded", "tests/test_control_plane_v1_1_1_candidate.py"),
    ]


def test_schedule_snapshot_is_full_metadata_and_exact_folded_dependencies() -> None:
    manifest = _load(V111)
    snapshot = _load(SNAPSHOT)
    assert snapshot["captured_at"] == CAPTURED_AT
    assert snapshot["capture"]["raw_projection_sha256"] == RAW_PROJECTION_SHA256
    assert snapshot["requirements_artifact"] == "docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md"
    assert _sha256(REQUIREMENTS) == manifest["schedule_requirements"]["sha256"]
    cards = {card["id"]: card for card in snapshot["cards"]}
    assert set(cards) == set(SCHEDULE_CARDS)
    for card_id, dependencies in SCHEDULE_CARDS.items():
        card = cards[card_id]
        assert card["dependencies"] == dependencies
        assert card["status"] == "backlog"
        assert card["kind"] == "task"
        assert card["swimlane"] == "feature"
        assert card["source"] == "cards"
        assert isinstance(card["title"], str) and card["title"]
        assert isinstance(card["description"], str) and card["description"]
        assert isinstance(card["labels"], list) and card["labels"]
        assert isinstance(card["acceptance_criteria"], list) and card["acceptance_criteria"]
        assert "created_at" in card and "updated_at" in card
    assert "c3a9c9e9" in cards["eddaa1fb"]["dependencies"]
    assert "4e1130cc" in cards["7888e091"]["dependencies"]


def test_catalog_lineage_and_paths_retain_every_gate() -> None:
    manifest = _load(V111)
    v110 = _load(V110)
    baseline = v110["schedule_catalog"]
    assert len(baseline) == 24
    assert manifest["catalog_lineage"] == {
        "baseline_manifest_path": "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json",
        "baseline_manifest_sha256": V110_SHA256,
        "baseline_catalog_card_count": 24,
        "discovered_schedule_contract_cards": ["c3a9c9e9", "4e1130cc"],
    }
    expected = {entry["card_id"] for entry in baseline} | set(SCHEDULE_CARDS) | set(LEGACY.values())
    paths = manifest["gate_topology"]["paths"]
    assert len(paths) == 29
    assert {entry["card_id"] for entry in paths} == expected
    for entry in paths:
        path = entry["path"]
        assert path[0] == entry["card_id"]
        assert path[-2:] == ["d0edbff1", "bea13a70"]
    edge = manifest["gate_topology"]["independent_review_human_gate_edge"]
    assert edge["card_id"] == "d0edbff1"
    assert edge["dependency"] == "bea13a70"
    assert edge["event_id"] == "c04a363468ed4efda9382fb497f195d6"


def test_ui_state_mapping_and_non_authorizations_remain_fail_closed() -> None:
    manifest = _load(V111)
    service = manifest["ui_contract_state_map"]["service"]
    assert service["query_key"] == "service"
    assert service["contract_field"] == "scope.service_id"
    states = manifest["ui_contract_state_map"]
    assert states["truth"]["mode"] == "typed_truth_state"
    assert states["preview"]["mode"] == "non_executing_exact_preview"
    assert states["proposal"]["mode"] == "evidence_linked_advisory_or_abstained"
    assert "No completion of human gate bea13a70 or independent review d0edbff1." in manifest[
        "consolidated_non_authorizations"
    ]


def test_v1_1_contracts_are_draft_2020_12_with_local_openapi_refs() -> None:
    contracts = ROOT / "docs" / "contracts" / "v1.1.0"
    documents = [_load(path) for path in sorted(contracts.glob("*.json"))]
    schemas = [document for document in documents if "$schema" in document]
    assert len(schemas) == 5
    for schema in schemas:
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)
    openapi = _load(contracts / "openapi.control-plane.v1.1.0.json")
    assert openapi["openapi"] == "3.1.0"
    for reference in _external_refs(openapi):
        assert not reference.startswith(("http://", "https://", "/")), reference
        assert (contracts / reference).is_file(), reference


def test_v1_1_1_candidate_text_contains_no_em_or_en_dash() -> None:
    manifest = _load(V111)
    paths = [ROOT / artifact["path"] for artifact in manifest["artifacts"]]
    paths.extend([V111, SNAPSHOT, REQUIREMENTS, Path(__file__)])
    for path in paths:
        if path.suffix in {".md", ".json", ".py", ".html"}:
            text = path.read_text(encoding="utf-8")
            assert "\u2013" not in text, path
            assert "\u2014" not in text, path
