"""Exact-source checks for the V1.1.3 approval receipt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v1.json"

RECEIPT_SHA256 = "9a7e902774ea1b3dc5ac550766a2e21cd51bc31a6d04a475996c60cbc8cdad81"
RAW_SHA256 = "8756eeeb8075de8ac020c757f1c596739fcd6b4e5b221a7dd10b564044ddaa3e"
NORMALIZED_SHA256 = "3427620b09ac23049ade1894ebbd52d9213439a3e112704dad37f7bd013f3cbe"


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _normalize(raw: str) -> str:
    normalized = []
    for source_line in raw.splitlines():
        line = source_line.strip(" \t")
        if line.startswith("> "):
            line = line[2:]
        if line:
            normalized.append(line)
    return " ".join(normalized)


def test_receipt_and_source_text_hashes_are_exact() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    raw = receipt["source"]["raw_markdown"]
    normalized = _normalize(raw)
    assert _sha256(RECEIPT) == RECEIPT_SHA256
    assert _sha256_bytes(raw.encode()) == RAW_SHA256
    assert normalized == receipt["normalization"]["normalized_text"]
    assert _sha256_bytes(normalized.encode()) == NORMALIZED_SHA256
    assert receipt["normalization"]["normalized_text_sha256"] == NORMALIZED_SHA256


def test_source_normalization_is_sensitive_to_content_changes() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    raw = receipt["source"]["raw_markdown"]
    changed_case = _normalize(raw.replace("I approve", "I Approve", 1))
    changed_punctuation = _normalize(raw.replace("action.", "action!", 1))
    assert _sha256_bytes(changed_case.encode()) != NORMALIZED_SHA256
    assert _sha256_bytes(changed_punctuation.encode()) != NORMALIZED_SHA256


def test_unavailable_source_metadata_remains_unknown() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    source = receipt["source"]
    assert source["message_id"] is None
    assert source["message_timestamp"] is None
    assert "no stable source message identifier" in source["unavailable_metadata_reason"]


def test_decision_and_supersession_are_narrow_and_complete() -> None:
    receipt = json.loads(RECEIPT.read_text(encoding="utf-8"))
    decision = receipt["decision"]
    assert decision["result"] == "approve"
    assert decision["manifest_sha256"] == (
        "9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb"
    )
    assert decision["release"] == "v0.1.29"
    assert decision["deployment_authorized"] is False
    assert decision["external_action_authorized"] is False

    predecessors = receipt["attribution_supersession"]["predecessors"]
    for predecessor in predecessors:
        path = ROOT / predecessor["path"]
        assert path.is_file()
        assert _sha256(path) == predecessor["sha256"]
        assert predecessor["historical_bytes_preserved"] is True

    statuses = {item["attribution_status"] for item in predecessors}
    assert "nonverbatim_expansion_mislabeled_as_quote" in statuses
    assert "accurate_transcription_superseded_by_machine_receipt" in statuses
    assert "preserved_fail_finding_pending_r3" in statuses
