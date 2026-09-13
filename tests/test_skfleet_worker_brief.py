"""Golden coverage for complete worker brief evidence materialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from skcapstone.fleet import worker_brief
from skcapstone.fleet.worker_brief import (
    BriefEvidenceError,
    format_work_envelope,
    materialize_work_envelope,
    safe_brief_links,
    write_missing_report,
)

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_STATUS_DIGEST = "d6e82bd608e1e4b216f87ad8ad98a7729ba075977dd79e1fb69911cf234a6044"
PRODUCT_STATUS_PATCH_DIGEST = "9d1eff048cf28ba2a672db2a260c24d3454284ae32eee648ed235610aef4aaa2"


def _write_digest(root: Path, parent: str, name: str, content: bytes) -> str:
    path = root / "work" / parent / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return hashlib.sha256(content).hexdigest()


def test_84f2c6a1_preserves_source_scope_and_policy_byte_for_byte(tmp_path: Path) -> None:
    source_scope = (
        "installed fleet wrapper /home/skuser01/.local/bin/skfleet-worker-wrapper.py "
        "and canonical SKCapstone source checkout"
    )
    card = {
        "id": "84f2c6a1",
        "kind": "task",
        "title": "Recover bounded workers from provider request rejection",
        "description": "Source-only implementation and tests.",
        "created_by": "jarvis",
        "acceptance_criteria": ["Preserve model policy and seat identity."],
        "dependencies": [],
        "links": {"source_scope": source_scope},
    }
    envelope = materialize_work_envelope(
        card,
        labels=["codex-only", "source-only"],
        lane="codex",
        model="sk-xl-public",
        seat=None,
        evidence_root=tmp_path,
    )
    rendered = format_work_envelope(envelope)

    assert json.loads(rendered) == envelope
    assert envelope["source_scope"] == [source_scope]
    assert envelope["producer_identity"] == "jarvis"
    assert envelope["acceptance_criteria"] == card["acceptance_criteria"]
    assert envelope["model_policy"] == {
        "lane": "codex",
        "model": "sk-xl-public",
        "qwen_first_exclusive": False,
        "seat": None,
    }


def test_0a47c8e3_resolves_exact_candidate_paths_and_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "work/45d600a5/COMPLETION-EVIDENCE.md"
    patch_path = tmp_path / "work/45d600a5/PRODUCT-STATUS.patch"
    monkeypatch.setattr(
        worker_brief,
        "_artifact_paths",
        lambda _root, _parent, _digests: {
            PRODUCT_STATUS_DIGEST: str(evidence_path),
            PRODUCT_STATUS_PATCH_DIGEST: str(patch_path),
        },
    )
    card = {
        "id": "0a47c8e3",
        "kind": "task",
        "title": "Verify delivery waves and Astra-only route truth",
        "description": "No repair or deployment.",
        "created_by": "jarvis",
        "acceptance_criteria": ["Return hashed PASS or FAIL_CLOSED."],
        "dependencies": ["45d600a5"],
        "links": {
            "producer_identity": "codex-status-45d600a5",
            "candidate_evidence_sha256": PRODUCT_STATUS_DIGEST,
            "candidate_patch_sha256": PRODUCT_STATUS_PATCH_DIGEST,
        },
    }
    envelope = materialize_work_envelope(
        card,
        labels=["codex-only", "parent-45d600a5", "review"],
        lane="codex",
        model="sk-xl-public",
        seat="link",
        evidence_root=tmp_path,
    )

    assert envelope["producer_identity"] == "codex-status-45d600a5"
    assert envelope["dependencies"] == ["45d600a5"]
    assert envelope["evidence_links"]["candidate_evidence_sha256"] == PRODUCT_STATUS_DIGEST
    assert envelope["evidence_links"]["candidate_patch_sha256"] == PRODUCT_STATUS_PATCH_DIGEST
    assert envelope["evidence_paths"] == {
        PRODUCT_STATUS_DIGEST: str(evidence_path),
        PRODUCT_STATUS_PATCH_DIGEST: str(patch_path),
    }
    assert envelope["model_policy"]["seat"] == "link"


def test_missing_candidate_is_typed_and_report_is_immutable(tmp_path: Path) -> None:
    card = {
        "id": "0a47c8e3",
        "links": {"candidate_evidence_sha256": "a" * 64},
    }
    with pytest.raises(BriefEvidenceError) as raised:
        materialize_work_envelope(
            card,
            labels=["parent-45d600a5", "review"],
            lane="codex",
            model="sk-xl-public",
            seat="link",
            evidence_root=tmp_path,
        )

    report = raised.value.report
    assert report == {
        "card_id": "0a47c8e3",
        "kind": "worker-brief-evidence-missing",
        "missing_paths": [
            {
                "expected_root": str(tmp_path / "work/45d600a5"),
                "sha256": "a" * 64,
            }
        ],
    }
    first = write_missing_report(report, tmp_path)
    second = write_missing_report(report, tmp_path)
    assert first == second
    assert json.loads(first[0].read_text())["content_sha256"] == first[1]


def test_candidate_path_resolution_hashes_real_bytes(tmp_path: Path) -> None:
    digest = _write_digest(tmp_path, "45d600a5", "candidate.patch", b"candidate")
    card = {
        "id": "0a47c8e3",
        "links": {"candidate_patch_sha256": digest},
    }
    envelope = materialize_work_envelope(
        card,
        labels=["parent-45d600a5"],
        lane="codex",
        model="sk-xl-public",
        seat=None,
        evidence_root=tmp_path,
    )
    assert envelope["evidence_paths"] == {digest: str(tmp_path / "work/45d600a5/candidate.patch")}


def test_secret_shaped_links_never_reach_brief() -> None:
    links = {
        "producer_identity": "builder",
        "capability_token": "not-for-a-prompt",
        "credential_path": "/private/key",
        "source_scope": "repository source only",
    }
    assert safe_brief_links(links) == {
        "producer_identity": "builder",
        "source_scope": "repository source only",
    }


def test_format_is_deterministic_and_prohibits_unrestricted_grants(tmp_path: Path) -> None:
    card = {
        "id": "84f2c6a1",
        "title": "card",
        "acceptance_criteria": ["one", "two"],
        "dependencies": ["11111111"],
        "links": {},
    }
    kwargs = {
        "labels": ["source-only", "codex-only"],
        "lane": "codex",
        "model": "sk-xl-public",
        "seat": None,
        "evidence_root": tmp_path,
    }
    first = materialize_work_envelope(card, **kwargs)
    second = materialize_work_envelope(card, **kwargs)
    assert format_work_envelope(first) == format_work_envelope(second)
    assert "unrestricted-tool-grant" in first["prohibited_actions"]
    assert "deployment" in first["prohibited_actions"]


def test_selector_materializes_and_writes_brief_before_claim() -> None:
    source = (ROOT / "scripts/fleet/skfleet-rotate.py").read_text(encoding="utf-8")
    start = source.index("    try:\n        envelope = materialize_work_envelope(")
    claim = source.index(
        '    claim=subprocess.run([SKC,"coord","claim",cid,"--agent",name]', start
    )
    region = source[start:claim]

    assert "write_missing_report(" in region
    assert "WORKER_BRIEF_BLOCKED" in region
    assert "brief_handle.write(brief)" in region
    assert "continue" in region
    assert source.index("brief_handle.write(brief)", start) < claim
