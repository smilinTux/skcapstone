"""Tests for governed fleet and RSI workspace lifecycle."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from skcapstone.fleet.workspace_lifecycle import (
    WorkspaceProof,
    WorkspaceState,
    cleanup_decision,
    cleanup_worktree,
    decide_terminal_state,
    recovery_manifest,
    transition,
    write_manifest,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def proof(workspace: Path, **changes: object) -> WorkspaceProof:
    """Build a synthetic proof for state-only tests."""
    base = WorkspaceProof(
        card_id="8c4a9e21",
        claim_revision="claim-1",
        workspace=str(workspace),
        branch="card/8c4a9e21",
        head="a" * 40,
        porcelain_sha256="b" * 64,
        dirty_paths=0,
        untracked_paths=0,
        custody_kind="bundle",
        custody_locator="/synthetic/custody.json",
        custody_sha256="c" * 64,
        custody_reachable=True,
        completion_evidence_sha256="d" * 64,
        review_required=True,
        review_dependency="review123",
        review_status="PASS",
        active_processes=0,
        recovery_instructions=("verify artifact", "restore head"),
        outcome="success",
    )
    return replace(base, **changes)


def authoritative_proof(tmp_path: Path) -> tuple[WorkspaceProof, Path]:
    """Create a real Git workspace and independently stored evidence."""
    repository = tmp_path / "repository"
    workspace = tmp_path / "workspace"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "fixture@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Fixture"], check=True)
    (repository / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "-qm", "fixture"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            "-q",
            "-b",
            "card/8c4a9e21",
            str(workspace),
        ],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    completion = evidence / "completion.md"
    completion.write_text("Card: 8c4a9e21\nPASS_FOR_REVIEW\n", encoding="utf-8")
    review = evidence / "review.md"
    review.write_text("Card: review123\nVerdict: PASS\n", encoding="utf-8")
    custody = evidence / "custody.bundle"
    porcelain = hashlib.sha256(status).hexdigest()
    subprocess.run(
        ["git", "-C", str(workspace), "bundle", "create", str(custody), "HEAD"],
        check=True,
    )
    item = WorkspaceProof(
        card_id="8c4a9e21",
        claim_revision="claim-1",
        workspace=str(workspace),
        branch="card/8c4a9e21",
        head=head,
        porcelain_sha256=porcelain,
        dirty_paths=0,
        untracked_paths=0,
        custody_kind="bundle",
        custody_locator=str(custody),
        custody_sha256=_sha(custody),
        custody_reachable=True,
        completion_evidence_sha256=_sha(completion),
        review_required=True,
        review_dependency="review123",
        review_status="PASS",
        active_processes=0,
        recovery_instructions=("verify artifact", "restore head"),
        outcome="success",
        completion_evidence_locator=str(completion),
        review_evidence_locator=str(review),
        review_evidence_sha256=_sha(review),
    )
    return item, repository


def test_authoritative_evidence_becomes_cleanup_eligible(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    assert decide_terminal_state(item).state is WorkspaceState.HANDOFF_REQUIRED
    decision = cleanup_decision(item, repository=repository, process_counter=lambda _: 0)
    assert decision.state is WorkspaceState.CLEANUP_ELIGIBLE


@pytest.mark.parametrize("outcome", ["failure", "timeout", "blocked", "interrupted"])
def test_unsuccessful_work_is_recoverable_quarantine(tmp_path: Path, outcome: str) -> None:
    item = proof(tmp_path, outcome=outcome)
    decision = decide_terminal_state(item)
    assert decision.state is WorkspaceState.RECOVERABLE_QUARANTINE
    manifest = recovery_manifest(item, decision.state)
    content = {key: value for key, value in manifest.items() if key != "content_sha256"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    assert manifest["content_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_caller_assertions_cannot_forge_cleanup_eligibility(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    forged = replace(
        item, custody_locator="/does/not/exist", custody_reachable=True, active_processes=0
    )
    decision = cleanup_decision(forged, repository=repository, process_counter=lambda _: 0)
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert "custody-unverified" in decision.reasons


def test_live_process_is_derived_not_trusted(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    decision = cleanup_decision(item, repository=repository, process_counter=lambda _: 1)
    assert "active-processes" in decision.reasons


def test_review_status_is_verified_from_hashed_evidence(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    review = Path(item.review_evidence_locator or "")
    review.write_text("Card: review123\nVerdict: FAIL\n", encoding="utf-8")
    forged = replace(item, review_evidence_sha256=_sha(review), review_status="PASS")
    decision = cleanup_decision(forged, repository=repository, process_counter=lambda _: 0)
    assert "review-incomplete" in decision.reasons


def test_unique_head_requires_recoverable_custody(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    custody = Path(item.custody_locator or "")
    custody.write_bytes(b"not a git bundle")
    forged = replace(item, custody_sha256=_sha(custody))
    decision = cleanup_decision(forged, repository=repository, process_counter=lambda _: 0)
    assert "custody-unverified" in decision.reasons


def test_uncommitted_bytes_are_never_cleanup_eligible(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    (Path(item.workspace) / "uncommitted.txt").write_text("recover me\n", encoding="utf-8")
    decision = cleanup_decision(item, repository=repository, process_counter=lambda _: 0)
    assert {"workspace-state-mismatch", "dirty-or-untracked-bytes"} <= set(decision.reasons)


def test_manifest_retry_is_idempotent_and_collision_fails(tmp_path: Path) -> None:
    manifest = recovery_manifest(proof(tmp_path), WorkspaceState.HANDOFF_REQUIRED)
    path = tmp_path / "manifest.json"
    first = write_manifest(path, manifest)
    assert write_manifest(path, manifest) == first
    with pytest.raises(ValueError, match="collision"):
        write_manifest(path, {"different": True})


def test_cleanup_is_plan_only_by_default(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    result = cleanup_worktree(item, repository=repository, process_counter=lambda _: 0)
    assert result["state"] == WorkspaceState.CLEANUP_ELIGIBLE.value
    assert result["executed"] is False
    assert result["command"][-2:] == ["--", str(Path(item.workspace).resolve())]


def test_partial_cleanup_failure_reports_actual_missing_state(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)

    def runner(
        command: list[str] | tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if list(command)[-4:-2] == ["worktree", "remove"]:
            shutil.rmtree(item.workspace)
            return subprocess.CompletedProcess(command, 1, "", "partial failure")
        return subprocess.run(command, **kwargs)  # type: ignore[arg-type]

    result = cleanup_worktree(
        item,
        repository=repository,
        execute=True,
        receipt_path=tmp_path / "receipt.json",
        runner=runner,
        process_counter=lambda _: 0,
    )
    assert result["state"] == WorkspaceState.CLEANUP_FAILED.value
    assert result["recovery_status"] == "workspace-missing"
    assert result["path_exists_after"] is False


def test_cleanup_retry_reads_exact_immutable_receipt(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    receipt = tmp_path / "receipt.json"
    digest = recovery_manifest(item, WorkspaceState.CLEANUP_ELIGIBLE)["content_sha256"]
    write_manifest(
        receipt,
        {
            "state": WorkspaceState.CLEANED.value,
            "proof_sha256": digest,
            "bytes_reclaimed": 7,
            "recovery_status": "preserved",
        },
    )
    shutil.rmtree(item.workspace)
    result = cleanup_worktree(item, repository=repository, execute=True, receipt_path=receipt)
    assert result["state"] == WorkspaceState.CLEANED.value
    assert result["executed"] is False
    assert result["retry"] is True


def test_successful_cleanup_writes_receipt_and_retry_is_idempotent(tmp_path: Path) -> None:
    item, repository = authoritative_proof(tmp_path)
    receipt = tmp_path / "receipt.json"
    result = cleanup_worktree(
        item,
        repository=repository,
        execute=True,
        receipt_path=receipt,
        process_counter=lambda _: 0,
    )
    assert result["state"] == WorkspaceState.CLEANED.value
    assert receipt.is_file()
    retry = cleanup_worktree(item, repository=repository, execute=True, receipt_path=receipt)
    assert retry["state"] == WorkspaceState.CLEANED.value
    assert retry["retry"] is True
    assert retry["executed"] is False


def test_cleaned_is_terminal() -> None:
    with pytest.raises(ValueError, match="invalid workspace transition"):
        transition(WorkspaceState.CLEANED, WorkspaceState.ACTIVE)
