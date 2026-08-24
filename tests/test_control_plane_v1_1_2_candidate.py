"""Truth and sealing checks for the V1.1.2 review candidate."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.2.json"
RECEIPT = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.2.receipt.json"
CAPTURE = ROOT / "docs/review/SKCP-00-RELEVANT-BOARD-CAPTURE-v1.1.2.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_v1_1_2_is_proposed_and_pins_the_merged_f8_revision() -> None:
    manifest = _load(MANIFEST)
    assert manifest["status"] == "proposed_for_human_review"
    assert manifest["implementation_authorized"] is False
    assert manifest["active_contract_source_revision"] == "dcdd6b25df3663656e7d476ac848ffdf6e183c66"
    assert manifest["human_review"] == {
        "card_id": "bea13a70", "captured_status": "backlog", "status": "incomplete"
    }
    assert manifest["independent_review"]["card_id"] == "d0edbff1"


def test_v1_1_2_pins_every_active_artifact_exactly() -> None:
    manifest = _load(MANIFEST)
    for artifact in manifest["artifacts"]:
        path = ROOT / artifact["path"]
        assert path.is_file(), artifact["path"]
        assert _sha256(path) == artifact["sha256"], artifact["path"]


def test_detached_receipt_is_exact_and_non_recursive() -> None:
    manifest = _load(MANIFEST)
    receipt = _load(RECEIPT)
    assert receipt["manifest_sha256"] == _sha256(MANIFEST)
    assert receipt["non_recursive"] is True
    assert receipt["implementation_authorized"] is False
    assert RECEIPT.relative_to(ROOT).as_posix() not in {
        artifact["path"] for artifact in manifest["artifacts"]
    }


def test_historical_contract_bytes_and_parity_remain_explicit() -> None:
    manifest = _load(MANIFEST)
    lineage = ROOT / "docs/review/lineage/v1.1.0/docs/contracts"
    expected = {
        "CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md": "a048f1bd715e6243eb1d2d293b0187512b35055d5a82c8480d7aff2c2b060353",
        "v1.1.0/control-plane-action-preview.v1.1.0.schema.json": "8cfbabbe47b971f1e2b8ccc4c90ff77a17701caf84d209f0a4fe2a257b47ae21",
        "v1.1.0/control-plane-insight.v1.1.0.schema.json": "3625513fe1e9e870deabadac840c76c7049638645d2b6d490d884deb2c871737",
        "v1.1.0/control-plane-metric-result.v1.1.0.schema.json": "a55d5de1db6241e5067a4d50c96a6bdd861b0b324785c54def7b92782de45c20",
        "v1.1.0/control-plane-recommendation.v1.1.0.schema.json": "d9a80faae276d2ce22a0189c1a03081dd2b293618e88838612df16895dd49fcd",
        "v1.1.0/control-plane-report-snapshot.v1.1.0.schema.json": "0758690146f4f937ff0c904a902f5324561f4b11cbd346a735535eb2c7e923e5",
        "v1.1.0/openapi.control-plane.v1.1.0.json": "32bed62acce4a12eea5c633e7ee0ffa5ebe35c5764dfbae3d1ccec6c333c1487",
    }
    for path, digest in expected.items():
        assert _sha256(lineage / path) == digest, path
    assert manifest["historical_parity"] == {
        "checked": 985, "matched": 590, "mismatches": 125, "missing": 270, "open_drift": 10
    }
    assert manifest["fresh_parity_observation"]["state"] == "unsafe"


def test_capture_proves_f8_done_and_all_gates_remain_closed() -> None:
    manifest = _load(MANIFEST)
    capture = _load(CAPTURE)
    cards = {card["id"]: card for card in capture["cards"]}
    assert capture["raw_projection"]["published"] is False
    assert capture["closure_algorithm"]["roots"] == [
        "26c69f86", "4e1130cc", "7888e091", "c3a9c9e9", "eddaa1fb"
    ]
    assert cards["26c69f86"]["status"] == "done"
    assert cards["bea13a70"]["status"] == "backlog"
    assert cards["d0edbff1"]["status"] == "review"
    assert manifest["captured_gate_statuses"] == {
        "26c69f86": "done", "ef91a99f": "ready", "bea13a70": "backlog", "d0edbff1": "review"
    }
    assert set(manifest["gate_paths"]) == {"c3a9c9e9", "eddaa1fb", "7888e091", "4e1130cc"}
    assert all(path == ["bea13a70", "d0edbff1"] for path in manifest["gate_paths"].values())


def test_recoverable_historical_bytes_are_archived_exactly() -> None:
    expected = {
        "docs/review/lineage/v1.1.0/docs/wireframes/control-plane-estate-pulse-v2.png": "33c400d4d4546e120a2662d5ef887d27ee85e4b87f5bdd973e038114d5e8c129",
        "docs/review/lineage/v1.1.0/docs/wireframes/control-plane-authorization-preview-v2.png": "f1ddf830f41a052917aeab6640183f649c0c8937cf7c441c5f2d1ef3d87463a8",
        "docs/review/lineage/v1.1.0/docs/evidence/SKCP-00F3F-COMPLETION-DEPENDENCY-GATE-2026-08-23.md": "f4e7e4404196da0fa43dd3fce2938d0ff9a36137254e706093d23f439ab16fae",
    }
    for path, digest in expected.items():
        assert _sha256(ROOT / path) == digest, path
