"""Fail-closed schedule truth and dialog checks for the F9 repair."""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD_SCHEDULE = ROOT / "docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.1.md"
SCHEDULE = ROOT / "docs/review/SKCP-00-SCHEDULE-REQUIREMENTS-v1.1.2.md"
OLD_WIREFRAME = ROOT / "docs/wireframes/control-plane-estate-pulse-v2.html"
WIREFRAME = ROOT / "docs/wireframes/control-plane-estate-pulse-v2.1.html"
EVIDENCE = ROOT / "docs/evidence/SKCP-00F9-SCHEDULE-TRUTH-REPAIR-2026-08-24.md"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted((_relative_luminance(first), _relative_luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _assert_repaired(schedule: str, wireframe: str) -> None:
    assert "policy-filtered record is shown as `not_applicable`" not in schedule
    assert "preserves its source `truth_state`" in schedule
    assert "`visibility.state: policy_filtered`" in schedule
    assert "access decision is never mapped to `not_applicable`" in schedule

    legal_row = re.search(r'<tr data-silo="legal">(.*?)</tr>', wireframe, flags=re.S)
    assert legal_row
    assert 'class="state unknown">Unknown</span>' in legal_row.group(1)
    assert "Visibility: policy filtered, authorization denied" in legal_row.group(1)
    assert 'class="state not_applicable">Policy filtered</span>' not in wireframe
    assert "Policy filtered visibility</span>source truth preserved" in wireframe
    assert '"unknown", "Policy-filtered global aggregate only"' in wireframe
    assert 'legal: "policy_filtered; authorization denied; source truth preserved"' in wireframe

    for drawer in ("evidence-drawer", "auth-drawer"):
        assert re.search(rf'id="{drawer}"[^>]+aria-hidden="true" hidden inert', wireframe)
    assert 'id="drawer-backdrop" aria-hidden="true" hidden' in wireframe
    for statement in (
        "drawer.hidden = false",
        "drawer.inert = false",
        "activeDrawer.hidden = true",
        "activeDrawer.inert = true",
        "backdrop.hidden = true",
    ):
        assert statement in wireframe


def _dump_dom(query: str) -> str:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("Chrome is unavailable for the synthetic interaction lane")
    result = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=1000",
            "--dump-dom",
            f"file://{WIREFRAME}?{query}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return html.unescape(result.stdout)


def test_approved_v1_1_2_inputs_remain_byte_exact() -> None:
    assert (
        _sha256(OLD_SCHEDULE) == "88172dd498f3071d7665dd1f5e37933dd229d808c6f3cc78b0ace14ce1b9b0ff"
    )
    assert (
        _sha256(OLD_WIREFRAME)
        == "66d007a9f1339929666e2a34586c1d49eb7e3d6236d83d11f43a449cf02b4c63"
    )


def test_repair_evidence_pins_superseding_artifacts() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    assert _sha256(SCHEDULE) == "b1f05fd98aa1d9dc940302321efcf57b5209a8020a1cff02ab658b3e5ec0911e"
    assert _sha256(WIREFRAME) == "b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9"
    assert _sha256(SCHEDULE) in evidence
    assert _sha256(WIREFRAME) in evidence
    assert "production Gantt workspace" in evidence


def test_schedule_and_wireframe_repair_is_sensitive_to_original_failures() -> None:
    _assert_repaired(
        SCHEDULE.read_text(encoding="utf-8"),
        WIREFRAME.read_text(encoding="utf-8"),
    )
    with pytest.raises(AssertionError):
        _assert_repaired(
            OLD_SCHEDULE.read_text(encoding="utf-8"),
            OLD_WIREFRAME.read_text(encoding="utf-8"),
        )


def test_ask_ai_button_meets_normal_text_contrast_and_detects_original_failure() -> None:
    assert _contrast("#ffffff", "#9a8cff") == pytest.approx(2.77438747245)
    assert _contrast("#ffffff", "#9a8cff") < 4.5
    assert _contrast("#06111e", "#9a8cff") >= 4.5
    text = WIREFRAME.read_text(encoding="utf-8")
    assert ".btn.purple { color: #06111e;" in text


def test_browser_preserves_truth_visibility_and_closed_dialog_focus() -> None:
    initial = _dump_dom("")
    assert re.search(r'id="evidence-drawer"[^>]+hidden=""[^>]+inert=""', initial)
    assert re.search(r'id="auth-drawer"[^>]+hidden=""[^>]+inert=""', initial)

    evidence = _dump_dom("evidence=legal")
    evidence_drawer = re.search(
        r'<aside class="drawer open" id="evidence-drawer".*?</aside>', evidence, re.S
    )
    assert evidence_drawer
    assert 'aria-hidden="false"' in evidence_drawer.group(0)
    assert " hidden=" not in evidence_drawer.group(0)
    assert " inert=" not in evidence_drawer.group(0)
    assert '<strong id="evidence-truth">unknown</strong>' in evidence
    assert (
        '<strong id="evidence-visibility">policy_filtered; authorization denied; '
        "source truth preserved</strong>" in evidence
    )
    assert re.search(r'id="auth-drawer"[^>]+hidden=""[^>]+inert=""', evidence)
