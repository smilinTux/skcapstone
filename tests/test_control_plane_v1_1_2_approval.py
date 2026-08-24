"""Exact-hash checks for the V1.1.2 human approval record."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.2.json"
RECEIPT = ROOT / "docs/review/SKCP-00-CANDIDATE-MANIFEST-v1.1.2.receipt.json"
APPROVAL = ROOT / "docs/approval/SKCP-00-V1.1.2-MEASUREMENT-ARCHITECTURE-APPROVAL-2026-08-24.md"

MANIFEST_SHA256 = "257db46aa26297873cd6a769e3f0eb7e6e3cf756224f99ef9a3aad61a45ff5ab"
RECEIPT_SHA256 = "46b98341094cf06a5f260c0ad1eed1e8d3a0090f27c2f8d570dcb84312028749"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_approval_names_the_exact_released_candidate() -> None:
    text = APPROVAL.read_text(encoding="utf-8")
    assert _sha256(MANIFEST) == MANIFEST_SHA256
    assert _sha256(RECEIPT) == RECEIPT_SHA256
    assert f"manifest sha256:{MANIFEST_SHA256}" in text
    assert f"Detached receipt SHA-256: `{RECEIPT_SHA256}`" in text
    assert "authorize implementation through the existing dependency gates" in text


def test_approval_preserves_review_and_non_authorization_gates() -> None:
    text = APPROVAL.read_text(encoding="utf-8")
    required = (
        "Independent review card `d0edbff1`",
        "does not complete `d0edbff1`",
        "Production deployment or tailnet ingress",
        "Protected SKLegal Matter retrieval or model egress",
        "HammerTime Inbox search, read, move, or processing",
        "Skipping CapAuth",
        "Gantt and schedule implementation remain blocked",
    )
    for statement in required:
        assert statement in text
