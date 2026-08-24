"""Exact-hash checks for the V1.1.3 source receipt v2 attestation."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v2.json"
ATTESTATION = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-V2-ATTESTATION-2026-08-24.md"
RECEIPT_SHA256 = "bf1c9d48c7721857d19f522a7aa36780f0a9fdb6cfa2c5a7bd6317c25fd213d3"
MERGE_REVISION = "d06cdc881ac4ff58a498a47d8add4b12f062567e"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_attestation_binds_the_exact_merged_v2_receipt() -> None:
    text = ATTESTATION.read_text(encoding="utf-8")
    assert _sha256(RECEIPT) == RECEIPT_SHA256
    assert RECEIPT_SHA256 in text
    assert MERGE_REVISION in text
    assert "V2 contains my exact extended approval text" in text
    assert "V1 remains rejected" in text


def test_attestation_preserves_narrow_scope() -> None:
    text = ATTESTATION.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert "only completion of `8a2331a2` and independent review `526bb17f`" in normalized
    for denied in (
        "deployment",
        "activation",
        "restart",
        "external action",
        "protected Matter access",
        "board reconciliation",
        "safety-gate bypass",
    ):
        assert denied in text
