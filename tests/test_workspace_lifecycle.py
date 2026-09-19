"""Tests for governed fleet workspace cleanup custody (Plan B1 vocabulary).

Ported from stranded commit a45aed76 (chiap08 salvage,
origin/fix/68a14a4f-seat-model-repoint) and reworked. The original module
proved custody through caller-supplied bundle/commit-locator fields that
every real caller left at None, and hardcoded review_required=True for a
review-by-default world Plan B1 (commit 621c358d) replaced with "push a
branch, PR only for sensitive cards". These tests instead prove custody
against the durable record B1 actually introduced: a card's own commit_sha
and branch links, read straight off CardStore, including the literal "none"
sentinel for a card that needed no repository change.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.fleet.workspace_lifecycle import (
    WorkspaceProof,
    WorkspaceState,
    card_custody,
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
    """Build a synthetic proof for state-only tests (no live workspace)."""
    base = WorkspaceProof(
        card_id="8c4a9e21",
        claim_revision="claim-1",
        workspace=str(workspace),
        branch="card/8c4a9e21",
        head="a" * 40,
        porcelain_sha256="b" * 64,
        dirty_paths=0,
        untracked_paths=0,
        active_processes=0,
        recovery_instructions=("verify artifact", "restore head"),
        outcome="success",
    )
    return replace(base, **changes)


def _make_card(home: Path, card_id: str) -> CardStore:
    """Create one minimal coordination home with an empty card, no links yet."""
    home.mkdir(parents=True, exist_ok=True)
    store = CardStore(home)
    store.create(CardCore(id=card_id, title="synthetic fleet card"))
    return store


def authoritative_workspace(tmp_path: Path) -> tuple[WorkspaceProof, Path, Path]:
    """Create a real Git workspace (no card, no links) for state-only checks."""
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
    porcelain = hashlib.sha256(status).hexdigest()
    item = WorkspaceProof(
        card_id="8c4a9e21",
        claim_revision="claim-1",
        workspace=str(workspace),
        branch="card/8c4a9e21",
        head=head,
        porcelain_sha256=porcelain,
        dirty_paths=0,
        untracked_paths=0,
        active_processes=0,
        recovery_instructions=("verify artifact", "restore head"),
        outcome="success",
    )
    return item, repository, workspace


def authoritative_proof(tmp_path: Path) -> tuple[WorkspaceProof, Path, Path]:
    """Create a real Git workspace AND a real card with commit_sha/branch links.

    Matches Plan B1's actual delivery record: skcapstone coord link <card>
    commit_sha <the-genuine-sha> and skcapstone coord link <card> branch
    <repo>:<name>. The branch link is deliberately the *card's own* branch,
    qualified with a synthetic repo name, mirroring convention documented in
    scripts/fleet/skfleet-rotate.py's worker prompt.
    """
    item, repository, workspace = authoritative_workspace(tmp_path)
    home = tmp_path / "coord-home"
    store = _make_card(home, item.card_id)
    store.append_event(item.card_id, "link", "worker", link_key="commit_sha", link_value=item.head)
    store.append_event(
        item.card_id, "link", "worker", link_key="branch", link_value=f"fixture:{item.branch}"
    )
    return item, repository, home


def test_card_custody_satisfied_by_genuine_sha_and_branch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _make_card(home, "8c4a9e21")
    store.append_event("8c4a9e21", "link", "worker", link_key="commit_sha", link_value="a" * 40)
    store.append_event(
        "8c4a9e21", "link", "worker", link_key="branch", link_value="fixture:card/8c4a9e21"
    )
    satisfied, detail = card_custody(home, "8c4a9e21")
    assert satisfied is True
    assert detail == "commit_sha-and-branch-linked"


def test_card_custody_satisfied_by_none_sentinel(tmp_path: Path) -> None:
    """The literal 'none' sentinel means the card needed no repository change."""
    home = tmp_path / "home"
    store = _make_card(home, "8c4a9e21")
    store.append_event("8c4a9e21", "link", "worker", link_key="commit_sha", link_value="none")
    satisfied, detail = card_custody(home, "8c4a9e21")
    assert satisfied is True
    assert detail == "no-repository-change"


def test_card_custody_refused_with_neither_link(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "8c4a9e21")
    satisfied, detail = card_custody(home, "8c4a9e21")
    assert satisfied is False
    assert detail == "no-commit_sha-link"


def test_card_custody_refused_for_genuine_sha_missing_branch(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _make_card(home, "8c4a9e21")
    store.append_event("8c4a9e21", "link", "worker", link_key="commit_sha", link_value="a" * 40)
    satisfied, detail = card_custody(home, "8c4a9e21")
    assert satisfied is False
    assert detail == "commit_sha-without-branch"


def test_card_custody_refused_for_placeholder_commit_sha(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _make_card(home, "8c4a9e21")
    store.append_event("8c4a9e21", "link", "worker", link_key="commit_sha", link_value="wip")
    satisfied, detail = card_custody(home, "8c4a9e21")
    assert satisfied is False
    assert detail == "commit_sha-not-a-genuine-sha-or-none"


def test_card_custody_refused_for_unknown_card(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    satisfied, detail = card_custody(home, "00000000")
    assert satisfied is False
    assert detail == "card-not-found"


def test_valid_commit_sha_plus_branch_permits_cleanup(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.CLEANUP_ELIGIBLE
    assert decision.reasons == ("all-gates-passed",)


def test_commit_sha_linked_to_none_permits_cleanup(tmp_path: Path) -> None:
    item, repository, workspace = authoritative_workspace(tmp_path)
    home = tmp_path / "coord-home"
    store = _make_card(home, item.card_id)
    store.append_event(item.card_id, "link", "worker", link_key="commit_sha", link_value="none")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.CLEANUP_ELIGIBLE


def test_card_with_neither_link_is_refused(tmp_path: Path) -> None:
    item, repository, workspace = authoritative_workspace(tmp_path)
    home = tmp_path / "coord-home"
    _make_card(home, item.card_id)
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any(reason.startswith("custody-unverified") for reason in decision.reasons)


def test_caller_cannot_forge_custody_on_the_proof_itself(tmp_path: Path) -> None:
    """WorkspaceProof carries no custody field at all: nothing on it to forge.

    This is the exact bug being fixed: the ported module used to accept
    custody_kind/custody_locator/custody_sha256/custody_reachable straight
    from the caller, and every real caller left them None forever. Asserting
    the dataclass has no such field is a regression guard against
    reintroducing that shape.
    """
    assert not hasattr(WorkspaceProof(**{**proof(tmp_path).__dict__}), "custody_kind")


@pytest.mark.parametrize("outcome", ["failure", "timeout", "blocked", "interrupted"])
def test_unsuccessful_work_is_recoverable_quarantine(tmp_path: Path, outcome: str) -> None:
    item = proof(tmp_path, outcome=outcome)
    decision = decide_terminal_state(item)
    assert decision.state is WorkspaceState.RECOVERABLE_QUARANTINE
    manifest = recovery_manifest(item, decision.state)
    content = {key: value for key, value in manifest.items() if key != "content_sha256"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    assert manifest["content_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_live_process_is_derived_not_trusted(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 1
    )
    assert "active-processes" in decision.reasons


def test_unique_head_requires_custody_from_the_card_not_the_proof(tmp_path: Path) -> None:
    """A workspace with pristine Git identity is still refused with no card links.

    Git identity alone (correct HEAD, correct branch, clean tree) used to be
    enough to satisfy the old custody check when the caller-set locator
    happened to be readable. It is not enough now: custody comes only from
    the card's own commit_sha/branch links.
    """
    item, repository, workspace = authoritative_workspace(tmp_path)
    home = tmp_path / "coord-home"
    _make_card(home, item.card_id)
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any("custody-unverified" in reason for reason in decision.reasons)


def test_uncommitted_bytes_are_never_cleanup_eligible(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)
    workspace = Path(item.workspace)
    (workspace / "uncommitted.txt").write_text("recover me\n", encoding="utf-8")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert {"workspace-state-mismatch", "dirty-or-untracked-bytes"} <= set(decision.reasons)


def test_manifest_retry_is_idempotent_and_collision_fails(tmp_path: Path) -> None:
    manifest = recovery_manifest(proof(tmp_path), WorkspaceState.HANDOFF_REQUIRED)
    path = tmp_path / "manifest.json"
    first = write_manifest(path, manifest)
    assert write_manifest(path, manifest) == first
    with pytest.raises(ValueError, match="collision"):
        write_manifest(path, {"different": True})


def test_cleanup_is_plan_only_by_default(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)
    result = cleanup_worktree(item, home=home, repository=repository, process_counter=lambda _: 0)
    assert result["state"] == WorkspaceState.CLEANUP_ELIGIBLE.value
    assert result["executed"] is False
    assert result["command"][-2:] == ["--", str(Path(item.workspace).resolve())]


def test_partial_cleanup_failure_reports_actual_missing_state(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)

    def runner(
        command: list[str] | tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if list(command)[-4:-2] == ["worktree", "remove"]:
            shutil.rmtree(item.workspace)
            return subprocess.CompletedProcess(command, 1, "", "partial failure")
        return subprocess.run(command, **kwargs)  # type: ignore[arg-type]

    result = cleanup_worktree(
        item,
        home=home,
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
    item, repository, home = authoritative_proof(tmp_path)
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
    result = cleanup_worktree(
        item, home=home, repository=repository, execute=True, receipt_path=receipt
    )
    assert result["state"] == WorkspaceState.CLEANED.value
    assert result["executed"] is False
    assert result["retry"] is True


def test_successful_cleanup_writes_receipt_and_retry_is_idempotent(tmp_path: Path) -> None:
    item, repository, home = authoritative_proof(tmp_path)
    receipt = tmp_path / "receipt.json"
    result = cleanup_worktree(
        item,
        home=home,
        repository=repository,
        execute=True,
        receipt_path=receipt,
        process_counter=lambda _: 0,
    )
    assert result["state"] == WorkspaceState.CLEANED.value
    assert receipt.is_file()
    retry = cleanup_worktree(
        item, home=home, repository=repository, execute=True, receipt_path=receipt
    )
    assert retry["state"] == WorkspaceState.CLEANED.value
    assert retry["retry"] is True
    assert retry["executed"] is False


def test_cleaned_is_terminal() -> None:
    with pytest.raises(ValueError, match="invalid workspace transition"):
        transition(WorkspaceState.CLEANED, WorkspaceState.ACTIVE)


def test_workspace_proof_has_no_review_required_field(tmp_path: Path) -> None:
    """review_required used to be hardcoded True by every real caller.

    Plan B1 made review opt-in (a PR only for sensitive cards), not the
    default, so this proof carries no review field to hardcode at all.
    """
    assert not hasattr(proof(tmp_path), "review_required")
