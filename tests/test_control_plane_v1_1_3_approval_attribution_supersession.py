"""Sensitivity checks for the append-only approval attribution supersession."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPROVAL = ROOT / "docs/approval/SKCP-00-V1.1.3-LINEAGE-CANDIDATE-APPROVAL-2026-08-24.md"
CONFLICT = ROOT / "docs/approval/SKCP-00-V1.1.3-LINEAGE-CANDIDATE-APPROVAL-CORRECTION-2026-08-24.md"
R2 = ROOT / "docs/review/SKCP-00-V1.1.3-INDEPENDENT-REREVIEW-2026-08-24.md"
SUPERSESSION = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-ATTRIBUTION-SUPERSESSION-2026-08-24.md"

HASHES = {
    APPROVAL: "7e4a84c70beb394c58493acb8e5e89ccfae24423dfeaee2351c17cd1fa5efc86",
    CONFLICT: "d34be0489b202e548ea6dfb033185a30ab3211b349c8de70701331b800d4f58d",
    R2: "4eec9f3211779a24e1299c42b3f762a395c578e39b99f466af0574941b96a42e",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_predecessor_records_remain_byte_exact() -> None:
    for path, expected in HASHES.items():
        assert _sha256(path) == expected


def test_supersession_preserves_exact_current_gate_approval() -> None:
    approval = APPROVAL.read_text(encoding="utf-8")
    supersession = SUPERSESSION.read_text(encoding="utf-8")
    exact = (
        "I APPROVE SKCP-00 V1.1.3 manifest SHA256 "
        "9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb "
        "at release v0.1.29 and merge revision "
        "e39b1b4cf2d546ea2c309174cce30b69eb43373c."
    )
    assert exact in approval
    assert exact in supersession
    assert "I confirm the archived PNG lineage narrative" in supersession


def test_conflicting_attribution_is_detected_and_superseded() -> None:
    conflict = CONFLICT.read_text(encoding="utf-8")
    supersession = SUPERSESSION.read_text(encoding="utf-8")
    normalized = " ".join(supersession.split())
    assert "did not type" in conflict
    assert "different text as verbatim" in normalized
    assert "attribution claim conflicts" in normalized
    assert "is superseded by" in normalized
    assert "introduces no new approval or authority" in normalized
    assert "does not authorize deployment" in normalized
