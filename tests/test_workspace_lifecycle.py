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
    """Create a real Git workspace AND a real card with every proof link.

    Matches Plan B1's actual delivery record: skcapstone coord claim,
    coord link <card> commit_sha <the-genuine-sha>, coord link <card> branch
    <repo>:<name>, and coord link <card> evidence <path>. The branch link is
    deliberately the *card's own* branch, qualified with a synthetic repo
    name, mirroring convention documented in scripts/fleet/skfleet-rotate.py's
    worker prompt. The claim event carries item.claim_revision so
    card_claim_generation sees the exact live generation, and the harness's
    HEAD is linked as commit_sha so unique_commit_custody can prove
    reachability.
    """
    item, repository, workspace = authoritative_workspace(tmp_path)
    home = tmp_path / "coord-home"
    store = _make_card(home, item.card_id)
    store.append_event(
        item.card_id, "claim", "worker", owner="worker", claim_revision=item.claim_revision
    )
    store.append_event(item.card_id, "link", "worker", link_key="commit_sha", link_value=item.head)
    store.append_event(
        item.card_id, "link", "worker", link_key="branch", link_value=f"fixture:{item.branch}"
    )
    store.append_event(
        item.card_id,
        "link",
        "worker",
        link_key="evidence",
        link_value="evidence/work/8c4a9e21/manifest.md",
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
    store.append_event(
        item.card_id, "claim", "worker", owner="worker", claim_revision=item.claim_revision
    )
    store.append_event(item.card_id, "link", "worker", link_key="commit_sha", link_value="none")
    store.append_event(
        item.card_id,
        "link",
        "worker",
        link_key="evidence",
        link_value="evidence/work/8c4a9e21/manifest.md",
    )
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


def test_transition_table_covers_every_governed_state() -> None:
    """Every named state is reachable and every transition is deterministic."""
    from skcapstone.fleet.workspace_lifecycle import TRANSITIONS

    assert set(TRANSITIONS) == set(WorkspaceState)
    # A successful handoff with a required review waits, then cleans.
    assert transition(WorkspaceState.ACTIVE, WorkspaceState.HANDOFF_REQUIRED) is (
        WorkspaceState.HANDOFF_REQUIRED
    )
    assert transition(WorkspaceState.HANDOFF_REQUIRED, WorkspaceState.REVIEW_PENDING) is (
        WorkspaceState.REVIEW_PENDING
    )
    assert transition(WorkspaceState.REVIEW_PENDING, WorkspaceState.CLEANUP_ELIGIBLE) is (
        WorkspaceState.CLEANUP_ELIGIBLE
    )
    # A failure path never jumps straight to cleanup.
    with pytest.raises(ValueError, match="invalid workspace transition"):
        transition(WorkspaceState.ACTIVE, WorkspaceState.CLEANUP_ELIGIBLE)
    with pytest.raises(ValueError, match="invalid workspace transition"):
        transition(WorkspaceState.RECOVERABLE_QUARANTINE, WorkspaceState.CLEANUP_ELIGIBLE)


def _record_verdict(home: Path, card_id: str, value: str) -> None:
    """Write one verdict row to the evidence store recorded_verdict reads."""
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    row = {"card_id": card_id, "link_key": "verdict", "link_value": value, "ts": "2026-01-01"}
    (events / "verdicts.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")


def _link(home: Path, card_id: str, key: str, value: str) -> None:
    CardStore(home).append_event(card_id, "link", "worker", link_key=key, link_value=value)


def _make_review_card(home: Path, review_id: str, parent_id: str) -> None:
    """Create a governed review card with exactly one parent label."""
    CardStore(home).create(
        CardCore(
            id=review_id,
            title=f"[REVIEW] synthetic review of {parent_id}",
            initial_labels=[f"parent-{parent_id}"],
        )
    )


def test_reviewer_handoff_waits_in_review_pending(tmp_path: Path) -> None:
    """A card linking an open review card must wait in REVIEW_PENDING."""
    item, repository, home = authoritative_proof(tmp_path)
    _make_review_card(home, "aaaa1111", item.card_id)
    _link(home, item.card_id, "review_card", "aaaa1111")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.REVIEW_PENDING
    assert any(reason.startswith("review-pending") for reason in decision.reasons)


def test_reviewer_handoff_terminal_pass_permits_cleanup(tmp_path: Path) -> None:
    """A terminal PASS on the linked review card discharges the dependency."""
    item, repository, home = authoritative_proof(tmp_path)
    _make_review_card(home, "aaaa1111", item.card_id)
    _link(home, item.card_id, "review_card", "aaaa1111")
    _record_verdict(home, "aaaa1111", "PASS")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.CLEANUP_ELIGIBLE


def test_reviewer_handoff_terminal_fail_still_permits_cleanup(tmp_path: Path) -> None:
    """A terminal FAIL is also discharged: the review is over, not silent."""
    item, repository, home = authoritative_proof(tmp_path)
    _make_review_card(home, "aaaa1111", item.card_id)
    _link(home, item.card_id, "review_card", "aaaa1111")
    _record_verdict(home, "aaaa1111", "FAIL")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.CLEANUP_ELIGIBLE


def test_reviewer_handoff_verdictless_review_stays_pending(tmp_path: Path) -> None:
    """Silence is not approval: a verdict-less review blocks cleanup."""
    item, repository, home = authoritative_proof(tmp_path)
    _make_review_card(home, "aaaa1111", item.card_id)
    _link(home, item.card_id, "review_card", "aaaa1111")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.REVIEW_PENDING


def test_stale_claim_generation_is_refused(tmp_path: Path) -> None:
    """A workspace whose claim moved to another generation must not be cleaned."""
    item, repository, home = authoritative_proof(tmp_path)
    stale = replace(item, claim_revision="an-older-generation")
    decision = cleanup_decision(
        stale, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any("stale-claim-generation" in reason for reason in decision.reasons)


def test_released_claim_is_refused(tmp_path: Path) -> None:
    """A card with no live claim has no running workspace to clean for."""
    item, repository, home = authoritative_proof(tmp_path)
    CardStore(home).append_event(
        item.card_id,
        "release_claim",
        "worker",
        released_owner="worker",
        expected_claim_revision=item.claim_revision,
    )
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any("claim-unverified" in reason for reason in decision.reasons)


def test_missing_evidence_link_is_refused(tmp_path: Path) -> None:
    """A workspace is the only copy of its bytes; unrecorded evidence blocks it."""
    item, repository, home = authoritative_proof(tmp_path)
    # Rebuild the card without the evidence link but with everything else.
    store = CardStore(home)
    store.append_event(item.card_id, "link", "worker", link_key="evidence", link_value="")
    decision = cleanup_decision(
        item, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any("evidence-unverified" in reason for reason in decision.reasons)


def test_unique_commit_not_reachable_from_custody_is_refused(tmp_path: Path) -> None:
    """A commit unique to the workspace with no custody ancestor is never deleted."""
    item, repository, home = authoritative_proof(tmp_path)
    workspace = Path(item.workspace)
    # Make a commit unique to the workspace branch, then re-link commit_sha to
    # the original ancestor so HEAD is no longer reachable from custody.
    (workspace / "unique.txt").write_text("unique\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "unique.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "unique work"], check=True)
    new_head = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # commit_sha still names item.head (the original ancestor), so the new
    # HEAD is not an ancestor of custody.
    _link(home, item.card_id, "commit_sha", item.head)
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
        check=True,
        capture_output=True,
    ).stdout
    dirty_proof = replace(
        item,
        head=new_head,
        porcelain_sha256=hashlib.sha256(status).hexdigest(),
    )
    decision = cleanup_decision(
        dirty_proof, home=home, repository=repository, process_counter=lambda _: 0
    )
    assert decision.state is WorkspaceState.HANDOFF_REQUIRED
    assert any("unique-commit-unverified" in reason for reason in decision.reasons)


def test_quarantine_manifest_carries_locator_and_reasons(tmp_path: Path) -> None:
    """A recoverable quarantine manifest points the next agent at exact bytes."""
    item = proof(tmp_path, outcome="interrupted")
    manifest = recovery_manifest(
        item,
        WorkspaceState.RECOVERABLE_QUARANTINE,
        reasons=("outcome:interrupted",),
        quarantine_locator=item.workspace,
    )
    assert manifest["quarantine_locator"] == item.workspace
    assert manifest["reasons"] == ["outcome:interrupted"]
    assert manifest["state"] == WorkspaceState.RECOVERABLE_QUARANTINE.value
    content = {key: value for key, value in manifest.items() if key != "content_sha256"}
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    assert manifest["content_sha256"] == hashlib.sha256(canonical).hexdigest()


def test_retry_after_successful_removal_without_receipt_is_deterministic(
    tmp_path: Path,
) -> None:
    """A missing, unregistered, already-clean workspace retries as CLEANED."""
    item, repository, home = authoritative_proof(tmp_path)
    subprocess.run(
        ["git", "-C", str(repository), "worktree", "remove", "--", item.workspace],
        check=True,
    )
    result = cleanup_worktree(item, home=home, repository=repository, process_counter=lambda _: 0)
    assert result["state"] == WorkspaceState.CLEANED.value
    assert result["retry"] is True
    assert result["executed"] is False


def test_workspace_proof_has_no_review_required_field(tmp_path: Path) -> None:
    """review_required used to be hardcoded True by every real caller.

    Plan B1 made review opt-in (a PR only for sensitive cards), not the
    default, so this proof carries no review field to hardcode at all.
    """
    assert not hasattr(proof(tmp_path), "review_required")
