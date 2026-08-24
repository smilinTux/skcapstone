"""Exact-text checks for the V1.1.3 source receipt attestation."""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ATTESTATION = (
    ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-ATTESTATION-2026-08-24.md"
)
SOURCE_RECEIPT = ROOT / "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v1.json"

EXACT_ATTESTATION = (
    "I attest SKCP-00 V1.1.3 approval source receipt "
    "sha256:d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18 "
    "at release v0.1.34 and revision "
    "01ae8021e1a070df53fa6fc283ad10df0a4a7ac9 is the authoritative "
    "representation of my approval text. The initial expanded quote was not "
    "verbatim. This authorizes only independent review cb8796b0 through "
    "existing dependency gates and does not authorize deployment or external "
    "action."
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _quote_text(text: str) -> str:
    lines = [line.removeprefix("> ") for line in text.splitlines() if line.startswith("> ")]
    return " ".join(lines)


def test_attestation_preserves_the_exact_human_words() -> None:
    text = ATTESTATION.read_text(encoding="utf-8")
    assert _quote_text(text) == EXACT_ATTESTATION
    assert "No additional statement is attributed to the human owner." in text


def test_attestation_binds_the_exact_receipt_and_scope() -> None:
    text = ATTESTATION.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    assert _sha256(SOURCE_RECEIPT) == (
        "d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18"
    )
    required = (
        "v0.1.34",
        "01ae8021e1a070df53fa6fc283ad10df0a4a7ac9",
        "3427620b09ac23049ade1894ebbd52d9213439a3e112704dad37f7bd013f3cbe",
        "authorizes only independent review `cb8796b0`",
        "does not authorize deployment",
        "external action",
        "gate bypass",
    )
    for statement in required:
        assert statement in normalized
