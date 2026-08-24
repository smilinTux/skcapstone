"""Exact-source and rejection checks for approval receipt v2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
V2 = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v2.json"
PREDECESSORS = {
    ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v1.json": "d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18",
    ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-ATTESTATION-2026-08-24.md": "dc1a54c080e98ffa0fa817109dc5d1eab438b92b367aa7a051aed82ef24dbab8",
    ROOT / "docs/approval/SKCP-00-V1.1.3-LINEAGE-CANDIDATE-APPROVAL-2026-08-24.md": "7e4a84c70beb394c58493acb8e5e89ccfae24423dfeaee2351c17cd1fa5efc86",
    ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-ATTRIBUTION-SUPERSESSION-2026-08-24.md": "1265c0df4edfdd4f722df028ae430f0b289decd97c5d1b9c94a10654020d8f57",
    ROOT / "docs/review/SKCP-00-V1.1.3-APPROVAL-ATTRIBUTION-R3-2026-08-24.md": "5e2009970d63adbe3f16d3621c2090d4e7ccd67d04493bcb4b74603a69dad843",
}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize(raw: str) -> str:
    lines = []
    for source_line in raw.splitlines():
        line = source_line.strip()
        if line.startswith(">"):
            line = line[1:]
            if line.startswith(" "):
                line = line[1:]
        if line:
            lines.append(line)
    return " ".join(lines)


def test_predecessor_bytes_remain_exact() -> None:
    for path, expected in PREDECESSORS.items():
        assert _sha256_bytes(path.read_bytes()) == expected


def test_v2_preserves_and_normalizes_the_extended_approval() -> None:
    receipt = json.loads(V2.read_text(encoding="utf-8"))
    raw = receipt["source"]["raw_markdown"]
    normalized = _normalize(raw)
    assert _sha256_bytes(raw.encode()) == receipt["source"]["raw_markdown_sha256"]
    assert normalized == receipt["normalization"]["normalized_text"]
    assert _sha256_bytes(normalized.encode()) == receipt["normalization"]["normalized_text_sha256"]
    assert "I confirm the archived PNG lineage narrative" in normalized
    assert "merge revision e39b1b4cf2d546ea2c309174cce30b69eb43373c" in normalized


def test_v1_rejection_and_authority_boundary_are_explicit() -> None:
    receipt = json.loads(V2.read_text(encoding="utf-8"))
    assert receipt["status"] == "proposed_for_exact_human_attestation"
    assert receipt["v1_rejection"]["rejected_receipt_sha256"] == PREDECESSORS[next(iter(PREDECESSORS))]
    assert receipt["v1_rejection"]["later_v1_attestation_status"] == "invalidated_by_prior_human_rejection"
    assert receipt["attribution_supersession"]["predecessors"][0]["attribution_status"] == "rejected_by_human_owner"
    assert receipt["decision"]["deployment_authorized"] is False
    assert receipt["decision"]["external_action_authorized"] is False
    assert set(receipt["non_authorizations"]) == {
        "deployment",
        "activation",
        "restart",
        "external_action",
        "protected_matter_access",
        "board_reconciliation",
        "gate_bypass",
    }
