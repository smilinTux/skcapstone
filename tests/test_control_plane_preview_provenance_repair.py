"""Sensitive checks for the F13 preview and authority projection repair."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASED_WIREFRAME = ROOT / "docs/wireframes/control-plane-estate-pulse-v2.1.html"
ACTIVE_WIREFRAME = ROOT / "docs/wireframes/control-plane-estate-pulse-v2.2.html"
PROJECTION = ROOT / "docs/approval/SKCP-00-V1.1.3-AUTHORITY-PROJECTION-v1.json"
CDP_QUALIFIER = ROOT / "scripts/qualify_control_plane_preview_cdp.mjs"
EVIDENCE = ROOT / "docs/evidence/SKCP-00F13-PREVIEW-PROVENANCE-REPAIR-2026-08-24.md"

AUTHORITATIVE = {
    "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v1.json": (
        "d6c5a0245ca42c3f32ffa73c3c0843154e66391ff40ad350ee58e3b7db91ac18"
    ),
    "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-ATTESTATION-2026-08-24.md": (
        "dc1a54c080e98ffa0fa817109dc5d1eab438b92b367aa7a051aed82ef24dbab8"
    ),
}
NON_AUTHORITATIVE = {
    "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-v2.json": (
        "bf1c9d48c7721857d19f522a7aa36780f0a9fdb6cfa2c5a7bd6317c25fd213d3"
    ),
    "docs/approval/SKCP-00-V1.1.3-APPROVAL-SOURCE-RECEIPT-V2-ATTESTATION-2026-08-24.md": (
        "84c99131840cebcc07cdea6d0020527107a92354d0ef2edf4a3d1a673da8fbe7"
    ),
    "docs/review/SKCP-00-V1.1.3-SOURCE-RECEIPT-V2-R4-2026-08-24.md": (
        "16b3842c8c957c6b3ef3c392b5c289a24aede0d571bb663f17726e331a9bb459"
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_fail_closed_source(text: str) -> None:
    assert 'unavailable: ["Preview state unavailable"' in text
    assert 'const state = previewStates[normalized] || previewStates.unavailable' in text
    assert 'previewState.value = previewStates[normalized] ? normalized : "unavailable"' in text
    assert 'setPreviewState(query.get("state"));' in text
    assert 'setPreviewState(query.get("state") || "ready");' not in text
    assert "const state = previewStates[stateName] || previewStates.ready" not in text
    assert 'function openAuth(event) { setPreviewState("ready");' in text


def _assert_authority_projection(data: dict[str, object]) -> None:
    authority = {item["path"]: item["sha256"] for item in data["authoritative_records"]}
    non_authority = {
        item["path"]: item["sha256"] for item in data["non_authoritative_records"]
    }
    assert authority == AUTHORITATIVE
    assert non_authority == NON_AUTHORITATIVE
    assert all(
        item["authority_status"] == "non_authoritative"
        for item in data["non_authoritative_records"]
    )
    assert data["history_policy"] == {
        "append_only": True,
        "historical_bytes_preserved": True,
        "conflicting_records_deleted": False,
    }
    assert data["scope"]["authorized"] == [
        "independent_review_cb8796b0_through_existing_dependency_gates"
    ]
    assert {item["fresh_review_gate_card_id"] for item in data["known_governance_limitations"]} == {
        "847e250a"
    }


def _dump_dom(query: str) -> str:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        pytest.skip("Chrome is unavailable for the synthetic browser lane")
    result = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=1000",
            "--dump-dom",
            f"file://{ACTIVE_WIREFRAME}?{query}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return html.unescape(result.stdout)


def _preview_result(query: str) -> tuple[str, str]:
    dom = _dump_dom(query)
    status = re.search(r'<strong id="auth-status-text">([^<]+)</strong>', dom)
    button = re.search(r'<button class="btn primary" id="authorize"[^>]*>', dom)
    assert status and button
    return status.group(1), button.group(0)


def test_released_v2_1_is_preserved_and_repair_is_sensitive() -> None:
    assert _sha256(RELEASED_WIREFRAME) == (
        "b3636c0017f5f3289094873b0ebed03806fbaa3bbc92bc705e03e0f7c32037c9"
    )
    _assert_fail_closed_source(ACTIVE_WIREFRAME.read_text(encoding="utf-8"))
    with pytest.raises(AssertionError):
        _assert_fail_closed_source(RELEASED_WIREFRAME.read_text(encoding="utf-8"))


def test_repair_evidence_names_and_pins_the_active_visual() -> None:
    evidence = EVIDENCE.read_text(encoding="utf-8")
    assert _sha256(ACTIVE_WIREFRAME) == (
        "f4722b9c77c8c6b1451aec7c59a4ac8c133635793e0ae4a1c558d9b09c128ce5"
    )
    assert _sha256(PROJECTION) == (
        "0e2fd4336f0ac58da3c0a50dcae11ecae5a233f2a35776b71aaea6d773780d5a"
    )
    assert _sha256(CDP_QUALIFIER) == (
        "e8a64ffea1ed055f331095ca437e6be26966444765c0ba114a19c35bbeec816b"
    )
    assert "V2.2 HTML file is the active control-plane" in evidence
    for path in (ACTIVE_WIREFRAME, PROJECTION, CDP_QUALIFIER):
        assert _sha256(path) in evidence


def test_unknown_missing_blank_and_whitespace_url_states_fail_closed_in_chrome() -> None:
    for query in (
        "preview=1&state=unsupported-state",
        "preview=1",
        "preview=1&state=",
        "preview=1&state=%20%20",
    ):
        status, button = _preview_result(query)
        assert status == "Preview state unavailable"
        assert 'disabled=""' in button
        assert 'aria-disabled="true"' in button


def test_declared_states_and_explicit_ready_url_keep_intended_behavior() -> None:
    expected = {
        "unavailable": ("Preview state unavailable", True),
        "ready": ("Ready for human authorization", False),
        "stale-target": ("Stale target", True),
        "denied-policy": ("Denied by policy", True),
        "expired": ("Expired preview", True),
        "changed-parameters": ("Changed parameters", True),
    }
    for state, (expected_status, disabled) in expected.items():
        status, button = _preview_result(f"preview=1&state={state}")
        assert status == expected_status
        assert ('disabled=""' in button) is disabled
        assert (f'aria-disabled="{str(disabled).lower()}"' in button) is True


def test_projection_pins_source_authority_and_rejects_widening() -> None:
    data = json.loads(PROJECTION.read_text(encoding="utf-8"))
    _assert_authority_projection(data)
    for path, digest in (AUTHORITATIVE | NON_AUTHORITATIVE).items():
        assert _sha256(ROOT / path) == digest

    widened = copy.deepcopy(data)
    widened["authoritative_records"].append(widened["non_authoritative_records"].pop())
    with pytest.raises(AssertionError):
        _assert_authority_projection(widened)


def test_projection_preserves_no_deployment_or_external_action_boundary() -> None:
    data = json.loads(PROJECTION.read_text(encoding="utf-8"))
    denied = set(data["scope"]["not_authorized"])
    assert {"deployment", "external_action", "gate_bypass"} <= denied
    assert "fetch(" not in ACTIVE_WIREFRAME.read_text(encoding="utf-8")
    assert "XMLHttpRequest" not in ACTIVE_WIREFRAME.read_text(encoding="utf-8")


def test_real_chrome_cdp_qualification() -> None:
    node = shutil.which("node")
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not node or not chrome:
        pytest.skip("Node or Chrome is unavailable for the CDP qualification wrapper")
    result = subprocess.run(
        [node, str(CDP_QUALIFIER)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "CHROME_PATH": chrome},
    )
    evidence = json.loads(result.stdout)
    assert evidence == {
        "result": "PASS",
        "userAgent": evidence["userAgent"],
        "failClosedUrlCases": 4,
        "declaredStates": 6,
        "explicitReadyTrigger": "PASS",
        "nonGetRequestsAfterClick": 0,
        "externalRequestsAfterClick": 0,
        "runtimeExceptions": 0,
    }
    assert "Chrome/" in evidence["userAgent"]
