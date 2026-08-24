from __future__ import annotations

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "docs" / "review"
ORIGINAL = REVIEW / "SKCP-00-CANDIDATE-MANIFEST.json"
SUPERSEDING = REVIEW / "SKCP-00-CANDIDATE-MANIFEST-v1.1.0.json"
ORIGINAL_SHA256 = "88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3"
F4_DEPENDENCIES = [
    "ee1d0874",
    "0242f9f2",
    "83a404bf",
    "079cd760",
    "f94fde82",
    "1f9ee2c9",
    "a081d5ed",
    "b24213ea",
    "8ab522ee",
    "bd732651",
    "50e36b06",
]
CATALOG = {
    "SKCP-11": ("9e88de5c", ["9508b8fd", "d0edbff1"]),
    "SKCP-12": ("804f14de", ["9508b8fd", "d0edbff1"]),
    "SKCP-13": (
        "c6828b8a",
        [
            "9508b8fd",
            "d0edbff1",
            "d12b8951",
            "9e88de5c",
            "804f14de",
            "5026359d",
            "08f4cdcb",
        ],
    ),
    "SKCP-14": ("5026359d", ["9508b8fd", "d0edbff1", "9e88de5c", "804f14de"]),
    "SKCP-15": ("08f4cdcb", ["9508b8fd", "d0edbff1", "9e88de5c"]),
    "SKCP-20": ("b7ada8b9", ["c6828b8a"]),
    "SKCP-21": ("5ee56779", ["804f14de", "b7ada8b9"]),
    "SKCP-21A": ("eddaa1fb", ["bea13a70", "d0edbff1", "b7ada8b9", "5ee56779"]),
    "SKCP-22": ("da097cbb", ["804f14de", "b7ada8b9"]),
    "SKCP-23": ("866ffaac", ["804f14de", "b7ada8b9"]),
    "SKCP-24": ("77d6bae0", ["804f14de", "b7ada8b9"]),
    "SKCP-25": ("b548a77a", ["5026359d", "b7ada8b9"]),
    "SKCP-30": ("169028ce", ["08f4cdcb", "5ee56779"]),
    "SKCP-30A": (
        "7888e091",
        ["bea13a70", "d0edbff1", "169028ce", "f080f150", "efa9bee8", "eddaa1fb"],
    ),
    "SKCP-31": (
        "f080f150",
        ["b7ada8b9", "5ee56779", "da097cbb", "866ffaac", "77d6bae0", "b548a77a"],
    ),
    "SKCP-31A": ("efa9bee8", ["f080f150"]),
    "SKCP-32": ("38731952", ["9e88de5c", "b7ada8b9"]),
    "SKCP-33": ("631f90bf", ["38731952", "94cbf19a"]),
    "SKCP-34": ("5858a34f", ["d12b8951", "38731952"]),
    "SKCP-40": ("008bd490", ["e6326000", "5858a34f", "d79100a7"]),
    "SKCP-41": ("cae1eaef", ["94cbf19a", "e6326000", "efa9bee8", "008bd490"]),
    "SKCP-50": (
        "83a8c40b",
        [
            "c6828b8a",
            "b7ada8b9",
            "5ee56779",
            "da097cbb",
            "866ffaac",
            "77d6bae0",
            "b548a77a",
        ],
    ),
    "SKCP-51": ("2d02b6ed", ["d12b8951", "804f14de", "38731952"]),
    "SKCP-52": ("ecf1148c", ["169028ce", "83a8c40b", "efa9bee8"]),
}
REMEDIATIONS = {
    "F1": "ee1d0874",
    "F1A": "bd732651",
    "F2": "0242f9f2",
    "F2A": "f94fde82",
    "F2B": "b24213ea",
    "F3": "83a404bf",
    "F3A": "079cd760",
    "F3B": "1f9ee2c9",
    "F3C": "a081d5ed",
    "F3D": "8ab522ee",
    "F3E": "50e36b06",
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _external_refs(value: object) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and not reference.startswith("#"):
            refs.append(reference.split("#", 1)[0])
        for child in value.values():
            refs.extend(_external_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.extend(_external_refs(child))
    return refs


def test_original_candidate_manifest_remains_byte_exact() -> None:
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == ORIGINAL_SHA256


def test_superseding_manifest_version_and_closed_review_gate() -> None:
    manifest = _load(SUPERSEDING)
    assert manifest["manifest_version"] == "2.0.0"
    assert manifest["manifest_schema_version"] == "2020-12"
    assert manifest["contract_version"] == "1.1.0"
    assert manifest["supersedes"]["path"] == "docs/review/SKCP-00-CANDIDATE-MANIFEST.json"
    assert manifest["supersedes"]["sha256"] == ORIGINAL_SHA256
    assert manifest["status"] == "proposed_for_human_review"
    assert manifest["implementation_authorized"] is False
    assert manifest["human_review"]["card_id"] == "bea13a70"
    assert manifest["independent_review"]["card_id"] == "d0edbff1"


def test_superseded_v1_1_0_manifest_defects_are_preserved_exactly() -> None:
    manifest = _load(SUPERSEDING)
    defects = []
    for artifact in manifest["artifacts"]:
        path = ROOT / artifact["path"]
        if not path.is_file():
            defects.append(("missing", artifact["path"]))
        elif hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
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
        ("superseded", "tests/test_control_plane_superseding_candidate.py"),
    ]


def test_versioned_contracts_are_draft_2020_12_and_openapi_refs_are_local() -> None:
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


def test_f4_dependency_event_fold_is_complete_and_exact() -> None:
    manifest = _load(SUPERSEDING)
    fold = manifest["f4_effective_dependency_fold"]
    assert fold["card_id"] == "90a02b0e"
    assert fold["effective_dependency_ids"] == F4_DEPENDENCIES
    assert len(fold["append_only_events"]) == 7
    assert [event["dependency"] for event in fold["append_only_events"]] == F4_DEPENDENCIES[4:]
    assert all(entry["status"] == "done" for entry in fold["effective_dependencies"])


def test_catalog_preserves_original_lineage_and_adds_schedule_cards() -> None:
    manifest = _load(SUPERSEDING)
    catalog = {entry["key"]: entry for entry in manifest["schedule_catalog"]}
    assert len(catalog) == 24
    assert set(catalog) == set(CATALOG)
    for key, (card_id, dependencies) in CATALOG.items():
        assert catalog[key]["card_id"] == card_id
        assert catalog[key]["dependencies"] == dependencies
    assert manifest["lineage"]["original_leaf_card_count"] == 22
    assert manifest["lineage"]["original_leaf_cards"] == _load(ORIGINAL)["leaf_cards"]
    assert manifest["lineage"]["additions"] == ["eddaa1fb", "7888e091"]


def test_remediation_traceability_pins_evidence_and_correct_descriptions() -> None:
    manifest = _load(SUPERSEDING)
    remediation = {entry["code"]: entry for entry in manifest["remediation_traceability"]}
    assert {code: entry["card_id"] for code, entry in remediation.items()} == REMEDIATIONS
    for entry in remediation.values():
        evidence = ROOT / entry["evidence_path"]
        assert evidence.is_file()
        assert entry["status"] == "complete"
    assert "independent-review dependency" in remediation["F3"]["description"]
    assert "dependency and claim-release" in remediation["F3B"]["description"]


def test_candidate_text_uses_ascii_dash_characters() -> None:
    manifest = _load(SUPERSEDING)
    paths = [ROOT / artifact["path"] for artifact in manifest["artifacts"]]
    paths.extend([SUPERSEDING, Path(__file__)])
    for path in paths:
        if path.suffix in {".md", ".json", ".py", ".html"}:
            text = path.read_text(encoding="utf-8")
            assert "\u2013" not in text, path
            assert "\u2014" not in text, path
