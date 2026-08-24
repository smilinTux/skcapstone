"""Attribution checks for the corrected V1.1.3 approval record."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORRECTION = (
    ROOT / "docs/approval/SKCP-00-V1.1.3-LINEAGE-CANDIDATE-APPROVAL-CORRECTION-2026-08-24.md"
)

EXACT_APPROVAL = (
    "I approve SKCP-00 V1.1.3 manifest "
    "sha256:9d06d085d4297cbad9a2f018daba091de011b98e3ed8eb788b59f80b8d36c4fb "
    "at release v0.1.29 and authorize independent rereview through the existing "
    "dependency gates. This does not authorize deployment or external action."
)


def _quote_text(text: str) -> str:
    lines = [line.removeprefix("> ") for line in text.splitlines() if line.startswith("> ")]
    return " ".join(lines)


def test_correction_preserves_the_exact_human_words() -> None:
    text = CORRECTION.read_text(encoding="utf-8")
    assert _quote_text(text) == EXACT_APPROVAL
    assert "No additional statement is attributed to the human owner." in text


def test_computed_metadata_is_separate_and_scope_stays_narrow() -> None:
    text = CORRECTION.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    required = (
        "repository evidence, not additional quoted human language",
        "e39b1b4cf2d546ea2c309174cce30b69eb43373c",
        "846ce6853fd386d549b7e2b4d5d7d1c1d985411be4529b6ca9a7c4fd8b42242c",
        "does not authorize deployment",
        "external action",
        "gate bypass",
    )
    for statement in required:
        assert statement in normalized
