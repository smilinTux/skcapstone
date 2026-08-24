"""Exact-hash checks for the append-only V1.1.3 lineage correction."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.3.json"
RECEIPT = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.3.receipt.json"
AMENDMENT = ROOT / "docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING-v1.1.3.md"
V112_ADR = ROOT / "docs/architecture/ADR-0001-CONTROL-PLANE-MEASUREMENT-AND-REPORTING-v1.1.2.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_prior_candidate_and_contradictory_narrative_remain_immutable() -> None:
    assert _sha256(V112_ADR) == (
        "fb3f5668e4d7d5c8db82bb6a2e74821944d91a9fec3c3db9b2dc74b58ab1ff0e"
    )
    assert (
        _sha256(ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.2.json")
        == "257db46aa26297873cd6a769e3f0eb7e6e3cf756224f99ef9a3aad61a45ff5ab"
    )
    assert "historical PNG wireframes are recorded only as unavailable" in (
        V112_ADR.read_text(encoding="utf-8")
    )


def test_amendment_truthfully_pins_retained_png_lineage() -> None:
    text = AMENDMENT.read_text(encoding="utf-8")
    expected = {
        "docs/review/lineage/v1.1.0/docs/wireframes/control-plane-estate-pulse-v2.png": "33c400d4d4546e120a2662d5ef887d27ee85e4b87f5bdd973e038114d5e8c129",
        "docs/review/lineage/v1.1.0/docs/wireframes/control-plane-authorization-preview-v2.png": "f1ddf830f41a052917aeab6640183f649c0c8937cf7c441c5f2d1ef3d87463a8",
    }
    for path, digest in expected.items():
        assert _sha256(ROOT / path) == digest
        assert path in text
        assert digest in text
    assert "present as retained lineage artifacts" in text


def test_manifest_pins_every_artifact_and_active_boundary() -> None:
    manifest = _load(MANIFEST)
    assert manifest["candidate_package_version"] == "1.1.3"
    assert manifest["status"] == "proposed_for_exact_human_review"
    assert manifest["implementation_authorized"] is False
    paths = {artifact["path"] for artifact in manifest["artifacts"]}
    assert {
        "docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.2.md",
        "docs/wireframes/control-plane-estate-pulse-v2.1.html",
        "docs/contracts/CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md",
        "docs/review/SKCP-00-V1.1.2-INDEPENDENT-REREVIEW-2026-08-24.md",
    } <= paths
    for artifact in manifest["artifacts"]:
        path = ROOT / artifact["path"]
        assert path.is_file(), artifact["path"]
        assert _sha256(path) == artifact["sha256"], artifact["path"]


def test_receipt_is_exact_non_recursive_and_requires_both_gates() -> None:
    manifest = _load(MANIFEST)
    receipt = _load(RECEIPT)
    assert receipt["manifest_sha256"] == _sha256(MANIFEST)
    assert receipt["non_recursive"] is True
    assert receipt["implementation_authorized"] is False
    assert manifest["human_review"] == {
        "card_id": "9ad1eeb8",
        "captured_status": "backlog",
        "status": "incomplete",
    }
    assert manifest["independent_review"] == {
        "card_id": "39085b32",
        "captured_status": "backlog",
        "status": "incomplete",
    }
    receipt_path = RECEIPT.relative_to(ROOT).as_posix()
    assert receipt_path not in {artifact["path"] for artifact in manifest["artifacts"]}
