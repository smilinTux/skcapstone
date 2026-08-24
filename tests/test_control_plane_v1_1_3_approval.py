"""Exact-hash checks for the V1.1.3 human approval record."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.3.json"
RECEIPT = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.3.receipt.json"
APPROVAL = ROOT / "docs/approval/SKCP-00-V1.1.3-LINEAGE-CANDIDATE-APPROVAL-2026-08-24.md"

MANIFEST_SHA256 = "9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb"
RECEIPT_SHA256 = "846ce6853fd386d549b7e2b4d5d7d1c1d985411be4529b6ca9a7c4fd8b42242c"
MERGE_REVISION = "e39b1b4cf2d546ea2c309174cce30b69eb43373c"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_approval_names_the_exact_released_candidate() -> None:
    text = APPROVAL.read_text(encoding="utf-8")
    assert _sha256(MANIFEST) == MANIFEST_SHA256
    assert _sha256(RECEIPT) == RECEIPT_SHA256
    assert MANIFEST_SHA256 in text
    assert RECEIPT_SHA256 in text
    assert MERGE_REVISION in text
    assert "`v0.1.29`" in text


def test_approval_preserves_review_and_non_authorization_gates() -> None:
    text = APPROVAL.read_text(encoding="utf-8")
    required = (
        "independent review `39085b32`",
        "does not authorize deployment",
        "protected Matter access",
        "board reconciliation",
        "safety-gate bypass",
        "without repairing the candidate",
    )
    for statement in required:
        assert statement in text
