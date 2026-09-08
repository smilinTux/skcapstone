from __future__ import annotations

import hashlib
from pathlib import Path

from skcapstone.qualification import AdmissionReason, admit_work_packet


def packet(tmp_path: Path) -> dict:
    candidate = tmp_path / "candidate.diff"
    evidence = tmp_path / "evidence.sha256"
    candidate.write_text("candidate\n")
    evidence.write_text("evidence\n")
    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "repository": "skcapstone",
        "candidate": {"path": str(candidate), "sha256": digest(candidate)},
        "evidence": {"path": str(evidence), "sha256": digest(evidence)},
        "dependencies": ["base"], "commands": ["pytest -q"],
        "expected_manifest": {"tests": "pass"}, "prohibited_effects": ["services"],
        "rollback": "discard worktree", "workspace": "read-only",
    }


def test_rejections(tmp_path):
    assert admit_work_packet(None).reason == AdmissionReason.EMPTY
    p = packet(tmp_path)
    assert (
        admit_work_packet({**p, "repository": "other"}, repository="skcapstone").reason
        == AdmissionReason.WRONG_REPOSITORY
    )
    assert (
        admit_work_packet({**p, "candidate": {"path": "no", "sha256": "x"}}).reason
        == AdmissionReason.MISSING_CANDIDATE
    )
    assert (
        admit_work_packet({**p, "dependencies": ["gone"]}, dependencies={}).reason
        == AdmissionReason.MISSING_DEPENDENCY
    )
    assert (
        admit_work_packet({**p, "commands": ["pytest; rm -rf /"]}).reason
        == AdmissionReason.UNVERIFIABLE_COMMAND
    )


def test_valid_packet_preserves_hashes(tmp_path):
    p = packet(tmp_path)
    result = admit_work_packet(p, dependencies={"base": True})
    assert result.admitted
    assert result.packet is p
    assert result.source_sha256 == p["candidate"]["sha256"]
    assert result.evidence_sha256 == p["evidence"]["sha256"]
