"""Govern preservation, quarantine, and exact fleet workspace cleanup.

Ported and reworked from stranded commit a45aed76 on
origin/fix/68a14a4f-seat-model-repoint (the chiap08 salvage). That commit
predates Plan B1 (commit 621c358d, "PR policy: push a branch by default, PR
only for sensitive cards") by 235 commits and spoke the world before it:
review was assumed mandatory, and "custody" meant a caller-supplied bundle or
commit-ref locator that every real caller left as None, so nothing was ever
actually verified.

Custody here means one thing: has the work reached somewhere that survives
deleting this workspace. Under Plan B1 that record is exactly two CardStore
links on the card itself (skcapstone.coord_completion, the same gate
`coord complete` and `coord move done` enforce): `commit_sha`, a genuine
40-character hex commit SHA or the literal sentinel "none" for a card that
needed no repository change, and `branch`, required alongside a genuine SHA
because it is the only part of the record that says what to fetch. See
`card_custody` below. A WorkspaceProof carries no custody field of its own to
assert or forge; cleanup_decision reads the card independently every time.

review is derived, not asserted. A card that links no review card owes no
review (Plan B1 made a PR opt-in, not a default), so it may leave
HANDOFF_REQUIRED for CLEANUP_ELIGIBLE directly. A card that links a review
card waits in REVIEW_PENDING until that review card's own recorded verdict
is a canonical terminal PASS or FAIL; an unreadable or verdict-less review
keeps the workspace out of CLEANUP_ELIGIBLE, because silence is not approval
(see skcapstone.review_verdict). review_required is not a proof field: it is
derived from the card's own `review_card` link by card_review_dependency.

Before CLEANUP_ELIGIBLE this module derives, from the card and Git rather than
from any caller assertion: the exact card and its live claim generation
(card_claim_generation), commit/branch custody that survives deletion
(card_custody), whether any workspace-unique commit already reached that
custody (unique_commit_custody), recorded completion evidence
(card_completion_evidence), the review dependency (card_review_dependency),
exact Git worktree branch/HEAD identity and a byte-exact porcelain hash, and
the absence of live processes inside the workspace. Any failure keeps the
workspace in RECOVERABLE_QUARANTINE, REVIEW_PENDING, or HANDOFF_REQUIRED;
none of those states deletes anything, and each manifest carries a hash-pinned
quarantine_locator pointing at the recoverable bytes.

cleanup_decision() is not merely defined and tested here: it is called from
scripts/fleet/skfleet-worker-wrapper.py's terminal path
(record_workspace_lifecycle_decision), which runs at the end of every fleet
worker's process, so it is reachable from real production traffic. Actual
deletion (cleanup_worktree(execute=True)) is deliberately left with no
production caller: see the module docs at docs/fleet/workspace-lifecycle.md
for why agents must never delete their own workspace, and the wrapper's own
docstring for the explicit `# intentionally-unwired` marker on execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Callable, Mapping, Sequence


class WorkspaceState(StrEnum):
    """Governed states for one exact worker workspace generation."""

    ACTIVE = "ACTIVE"
    HANDOFF_REQUIRED = "HANDOFF_REQUIRED"
    REVIEW_PENDING = "REVIEW_PENDING"
    RECOVERABLE_QUARANTINE = "RECOVERABLE_QUARANTINE"
    CLEANUP_ELIGIBLE = "CLEANUP_ELIGIBLE"
    CLEANED = "CLEANED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


#: Deterministic transition table. REVIEW_PENDING is where a successful
#: handoff waits while its required independent review is still open, and is
#: the only state that can enter CLEANUP_ELIGIBLE without the review already
#: being terminal. RECOVERABLE_QUARANTINE never deletes: its only forward edge
#: is back to HANDOFF_REQUIRED once custody and review are proven.
TRANSITIONS = {
    WorkspaceState.ACTIVE: {
        WorkspaceState.HANDOFF_REQUIRED,
        WorkspaceState.RECOVERABLE_QUARANTINE,
    },
    WorkspaceState.HANDOFF_REQUIRED: {
        WorkspaceState.REVIEW_PENDING,
        WorkspaceState.CLEANUP_ELIGIBLE,
        WorkspaceState.RECOVERABLE_QUARANTINE,
    },
    WorkspaceState.REVIEW_PENDING: {
        WorkspaceState.CLEANUP_ELIGIBLE,
        WorkspaceState.RECOVERABLE_QUARANTINE,
    },
    WorkspaceState.RECOVERABLE_QUARANTINE: {WorkspaceState.HANDOFF_REQUIRED},
    WorkspaceState.CLEANUP_ELIGIBLE: {
        WorkspaceState.CLEANED,
        WorkspaceState.CLEANUP_FAILED,
    },
    WorkspaceState.CLEANUP_FAILED: {WorkspaceState.CLEANUP_ELIGIBLE},
    WorkspaceState.CLEANED: set(),
}

_HEX = re.compile(r"^[0-9a-f]{40,64}$")

#: The B1 completion-evidence link keys and "no repository change" sentinel
#: (skcapstone.coord_completion) restated here as plain literals. That module
#: keeps _COMMIT_SHA_LINK_KEY, _BRANCH_LINK_KEY, and _NO_CHANGE_SENTINEL
#: underscore-private on purpose (they are its own gate's implementation
#: detail); the one thing it exports for reuse is commit_sha_is_valid, which
#: this module imports directly rather than copying. A rename on either side
#: without the matching rename here fails test_workspace_lifecycle.py's
#: fixtures immediately, not silently.
_COMMIT_SHA_LINK_KEY = "commit_sha"
_BRANCH_LINK_KEY = "branch"
_NO_CHANGE_SENTINEL = "none"


def _valid_hash(value: str | None) -> bool:
    return bool(value and _HEX.fullmatch(value.lower()))


def _workspace_process_count(workspace: Path) -> int:
    """Count live processes whose authoritative cwd is inside the workspace."""
    count = 0
    for entry in Path("/proc").glob("[0-9]*"):
        if entry.name == str(os.getpid()):
            continue
        try:
            cwd = (entry / "cwd").resolve(strict=True)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if cwd == workspace or workspace in cwd.parents:
            count += 1
    return count


def card_custody(home: Path, card_id: str) -> tuple[bool, str]:
    """Return whether ``card_id``'s own links prove custody survives deletion.

    Reads the card's folded CardStore links directly rather than trusting
    anything a caller could set on a WorkspaceProof: trusting the caller was
    the exact defect this module is being reworked to fix (see the module
    docstring). Matches skcapstone.coord_completion's commit-evidence gate,
    the one already enforced at `coord complete` and `coord move done`: a
    genuine commit_sha plus a non-blank branch, or the literal sentinel
    "none" recording that the card needed no repository change (no branch
    required there, since there is no code to fetch).
    """
    from skcapstone.card_store import CardStore
    from skcapstone.coord_completion import commit_sha_is_valid

    try:
        card = CardStore(Path(home).expanduser()).fold(card_id)
    except Exception:  # noqa: BLE001 - fail closed toward refusing cleanup
        return False, "card-unreadable"
    if card is None:
        return False, "card-not-found"
    links = dict(getattr(card, "links", None) or {})
    commit_sha = str(links.get(_COMMIT_SHA_LINK_KEY) or "").strip()
    if not commit_sha:
        return False, "no-commit_sha-link"
    if commit_sha == _NO_CHANGE_SENTINEL:
        return True, "no-repository-change"
    if not commit_sha_is_valid(commit_sha):
        return False, "commit_sha-not-a-genuine-sha-or-none"
    branch = str(links.get(_BRANCH_LINK_KEY) or "").strip()
    if not branch:
        return False, "commit_sha-without-branch"
    return True, "commit_sha-and-branch-linked"


def _fold_card(home: Path, card_id: str):
    """Return the folded card for ``card_id``, or None, without raising."""
    from skcapstone.card_store import CardStore

    try:
        return CardStore(Path(home).expanduser()).fold(card_id)
    except Exception:  # noqa: BLE001 - a broken fold must fail closed
        return None


def card_claim_generation(home: Path, card_id: str, claim_revision: str) -> tuple[bool, str]:
    """Prove the exact claim generation this workspace belongs to is still live.

    Reads ``owner`` and ``meta["_claim_revision"]`` off the folded card, the
    same authority the wrapper's own startup ownership fence uses. A workspace
    whose claim has moved to another generation must never be cleaned: a later
    claim may be running inside the same workspace path, and a stale proof is
    exactly how running work gets deleted. No claim revision is trusted from
    the proof as evidence of liveness; only the card is.
    """
    card = _fold_card(home, card_id)
    if card is None:
        return False, "card-unreadable"
    if getattr(card, "archived", False):
        return False, "card-archived"
    owner = str(getattr(card, "owner", None) or "").strip()
    live_revision = str((getattr(card, "meta", None) or {}).get("_claim_revision") or "")
    if not owner or not live_revision:
        return False, "claim-released"
    if live_revision != str(claim_revision or "").strip():
        return False, "stale-claim-generation"
    return True, "claim-generation-current"


def card_completion_evidence(home: Path, card_id: str) -> tuple[bool, str]:
    """Prove the card records immutable completion evidence before deletion.

    Reads the folded card's ``evidence`` link. A workspace is the only copy of
    its own bytes, so deleting it before the card names durable evidence is
    exactly the loss this lifecycle exists to prevent.
    """
    card = _fold_card(home, card_id)
    if card is None:
        return False, "card-unreadable"
    links = dict(getattr(card, "links", None) or {})
    value = str(links.get("evidence") or "").strip()
    return (bool(value), "evidence-linked" if value else "no-evidence-link")


def card_review_dependency(home: Path, card_id: str) -> tuple[str, str]:
    """Return ``(status, detail)`` for the card's required review dependency.

    Status is one of:

      - ``"none"``: the card links no review card, so Plan B1's opt-in review
        rule means no review is owed; cleanup is not blocked on review.
      - ``"pending"``: the card links a review card that is not yet terminal
        (or whose fold fails), so the workspace must wait in REVIEW_PENDING.
      - ``"terminal"``: the linked review card is archived/completed and its
        recorded verdict is a canonical terminal PASS or FAIL, so the review
        dependency is discharged.

    A review link that cannot be read or that has no terminal verdict is
    deliberately reported ``pending``, never ``terminal``: silence is not
    approval (see skcapstone.review_verdict), so an unreadable review keeps
    the workspace quarantined rather than authorising its deletion.
    """
    card = _fold_card(home, card_id)
    if card is None:
        return "pending", "card-unreadable"
    links = dict(getattr(card, "links", None) or {})
    review_id = str(links.get("review_card") or "").strip()
    if not review_id:
        return "none", "no-review-required"
    review = _fold_card(home, review_id)
    if review is None:
        return "pending", f"review-card-unreadable:{review_id}"
    from skcapstone.review_verdict import recorded_verdict

    verdict = str(recorded_verdict(review_id, Path(home).expanduser()) or "").strip()
    terminal = verdict == "PASS" or verdict == "FAIL" or verdict.startswith("BLOCKED ")
    if not terminal:
        return "pending", f"review-verdict-not-terminal:{review_id}"
    return "terminal", f"review-terminal:{review_id}:{verdict.split()[0]}"


def _git(
    runner: Callable[..., subprocess.CompletedProcess[str]],
    repository: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run one read-only Git verification command."""
    return runner(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _registered_worktrees(
    runner: Callable[..., subprocess.CompletedProcess[str]], repository: Path
) -> tuple[int, dict[Path, dict[str, str]]]:
    """Read exact worktree paths, heads, and branches from Git porcelain."""
    result = _git(runner, repository, "worktree", "list", "--porcelain", "-z")
    records: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for field in result.stdout.split("\0"):
        if field.startswith("worktree "):
            if current:
                records[Path(current["worktree"]).resolve()] = current
            current = {"worktree": field.removeprefix("worktree ")}
        elif " " in field and current:
            key, value = field.split(" ", 1)
            current[key] = value
    if current:
        records[Path(current["worktree"]).resolve()] = current
    return result.returncode, records


def unique_commit_custody(
    proof: WorkspaceProof,
    *,
    home: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    repository: Path,
) -> tuple[bool, str]:
    """Prove any commit unique to the workspace already reached durable custody.

    A workspace whose HEAD carries commits reachable from nowhere else is the
    one case a workspace delete is irreversible. This derives, rather than
    trusts, the custody commit named by the card's own ``commit_sha`` link
    (which cleanup_decision separately requires to be non-empty), then asks
    Git directly whether the workspace HEAD is an ancestor of it. A commit
    unique to the workspace with no custody ancestor is refused.
    """
    from skcapstone.coord_completion import commit_sha_is_valid

    card = _fold_card(home, proof.card_id)
    if card is None:
        return False, "card-unreadable"
    links = dict(getattr(card, "links", None) or {})
    custody_sha = str(links.get("commit_sha") or "").strip()
    if not custody_sha or custody_sha == _NO_CHANGE_SENTINEL:
        # No custody commit is linked. A workspace that still carries a commit
        # reachable from nowhere else (rev-list HEAD not reachable from any
        # ref) would be destroyed forever by a delete, so refuse it. A clean
        # workspace with no unique commit is safe.
        unique = _git(runner, repository, "rev-list", "--count", "HEAD", "--not", "--all")
        if unique.returncode != 0:
            return False, "unique-commit-check-unreadable"
        try:
            has_unique = int(unique.stdout.strip()) > 0
        except ValueError:
            return False, "unique-commit-check-unreadable"
        if has_unique:
            return False, "unique-commit-without-custody"
        return True, "no-unique-commit"
    if not commit_sha_is_valid(custody_sha):
        return False, "custody-sha-not-genuine"
    reachable = _git(
        runner,
        repository,
        "merge-base",
        "--is-ancestor",
        proof.head,
        custody_sha,
    )
    if reachable.returncode != 0:
        return False, "head-not-reachable-from-custody-commit"
    return True, "head-reachable-from-custody-commit"


@dataclass(frozen=True)
class WorkspaceProof:
    """Non-secret facts about one exact card, claim, and workspace generation.

    Custody is deliberately not a field here. The ported version carried
    custody_kind/custody_locator/custody_sha256/custody_reachable, and every
    real caller left them None forever, so custody was never actually
    checked; see the module docstring. cleanup_decision reads custody
    independently off the card via card_custody, so a proof cannot assert its
    own custody. review_required is dropped for the same reason it was
    hardcoded True and wrong: see the module docstring.
    """

    card_id: str
    claim_revision: str
    workspace: str
    branch: str
    head: str
    porcelain_sha256: str
    dirty_paths: int
    untracked_paths: int
    active_processes: int
    recovery_instructions: tuple[str, ...]
    outcome: str


@dataclass(frozen=True)
class LifecycleDecision:
    """A deterministic state decision with fail-closed reasons."""

    state: WorkspaceState
    reasons: tuple[str, ...]


def recovery_manifest(
    proof: WorkspaceProof,
    state: WorkspaceState,
    *,
    reasons: Sequence[str] = (),
    quarantine_locator: str | None = None,
) -> dict[str, object]:
    """Return a hash-pinned manifest without inspecting or changing the workspace.

    For a quarantine state the manifest carries an explicit
    ``quarantine_locator`` (defaulting to the exact workspace path itself, the
    one location a later agent must recover from) so subsequent agents are
    pointed at recoverable bytes instead of being left to guess. The locator
    is part of the hashed content, so it cannot be edited after the fact.
    """
    payload: dict[str, object] = {
        "schema": "skcapstone.workspace-recovery.v3",
        "state": state.value,
        "proof": asdict(proof),
        "reasons": list(reasons),
    }
    if state in {
        WorkspaceState.RECOVERABLE_QUARANTINE,
        WorkspaceState.HANDOFF_REQUIRED,
        WorkspaceState.REVIEW_PENDING,
    }:
        payload["quarantine_locator"] = (
            str(quarantine_locator) if quarantine_locator is not None else proof.workspace
        )
        payload["recovery_instructions"] = list(proof.recovery_instructions)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "content_sha256": hashlib.sha256(canonical).hexdigest()}


def decide_terminal_state(proof: WorkspaceProof) -> LifecycleDecision:
    """Classify terminal worker state without granting cleanup authority."""
    required = {
        "card_id": proof.card_id,
        "claim_revision": proof.claim_revision,
        "workspace": proof.workspace,
        "head": proof.head,
        "porcelain_sha256": proof.porcelain_sha256,
    }
    missing = tuple(sorted(key for key, value in required.items() if not value))
    invalid_hashes = tuple(
        key
        for key, value in (
            ("head", proof.head),
            ("porcelain_sha256", proof.porcelain_sha256),
        )
        if value and not _valid_hash(value)
    )
    if missing or invalid_hashes or proof.outcome != "success":
        reasons = missing + invalid_hashes + (() if proof.outcome != "success" else ())
        return LifecycleDecision(
            WorkspaceState.RECOVERABLE_QUARANTINE,
            reasons or (f"outcome:{proof.outcome}",),
        )
    return LifecycleDecision(WorkspaceState.HANDOFF_REQUIRED, ("preservation-required",))


def cleanup_decision(
    proof: WorkspaceProof,
    *,
    home: Path,
    repository: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_counter: Callable[[Path], int] = _workspace_process_count,
) -> LifecycleDecision:
    """Derive cleanup eligibility from CardStore custody, Git, and process state.

    ``home`` is the coordination home to read the card's commit_sha/branch
    links from (see card_custody); it is required, not optional, because
    custody can only be proved by reading the card, never by trusting the
    proof.
    """
    reasons: list[str] = []
    terminal = decide_terminal_state(proof)
    if terminal.state is not WorkspaceState.HANDOFF_REQUIRED:
        reasons.extend(terminal.reasons)
    if repository is None:
        reasons.append("authoritative-verification-required")
        return LifecycleDecision(WorkspaceState.HANDOFF_REQUIRED, tuple(reasons))
    try:
        workspace = Path(proof.workspace).resolve(strict=True)
        repository = repository.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return LifecycleDecision(WorkspaceState.HANDOFF_REQUIRED, ("workspace-unavailable",))
    returncode, registered = _registered_worktrees(runner, repository)
    record = registered.get(workspace)
    if returncode or record is None:
        reasons.append("workspace-not-registered")
    elif (
        record.get("HEAD") != proof.head
        or record.get("branch", "").removeprefix("refs/heads/") != proof.branch
    ):
        reasons.append("workspace-identity-mismatch")
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain=v1", "-z"],
        capture_output=True,
        check=False,
    )
    entries = [entry for entry in status.stdout.split(b"\0") if entry]
    dirty = sum(not entry.startswith(b"??") for entry in entries)
    untracked = sum(entry.startswith(b"??") for entry in entries)
    if status.returncode or hashlib.sha256(status.stdout).hexdigest() != proof.porcelain_sha256:
        reasons.append("workspace-state-mismatch")
    if dirty or untracked or dirty != proof.dirty_paths or untracked != proof.untracked_paths:
        reasons.append("dirty-or-untracked-bytes")
    if process_counter(workspace) != 0:
        reasons.append("active-processes")
    custody_ok, custody_detail = card_custody(home, proof.card_id)
    if not custody_ok:
        reasons.append(f"custody-unverified:{custody_detail}")
    claim_ok, claim_detail = card_claim_generation(home, proof.card_id, proof.claim_revision)
    if not claim_ok:
        reasons.append(f"claim-unverified:{claim_detail}")
    evidence_ok, evidence_detail = card_completion_evidence(home, proof.card_id)
    if not evidence_ok:
        reasons.append(f"evidence-unverified:{evidence_detail}")
    unique_ok, unique_detail = unique_commit_custody(
        proof, home=home, repository=repository, runner=runner
    )
    if not unique_ok:
        reasons.append(f"unique-commit-unverified:{unique_detail}")
    review_status, review_detail = card_review_dependency(home, proof.card_id)
    if review_status == "pending":
        return LifecycleDecision(
            WorkspaceState.REVIEW_PENDING, tuple(reasons) + (f"review-pending:{review_detail}",)
        )
    if not proof.recovery_instructions:
        reasons.append("recovery-instructions")
    if reasons:
        return LifecycleDecision(WorkspaceState.HANDOFF_REQUIRED, tuple(reasons))
    return LifecycleDecision(WorkspaceState.CLEANUP_ELIGIBLE, ("all-gates-passed",))


def transition(current: WorkspaceState, target: WorkspaceState) -> WorkspaceState:
    """Apply one deterministic lifecycle transition."""
    if target not in TRANSITIONS[current]:
        raise ValueError(f"invalid workspace transition: {current.value} -> {target.value}")
    return target


def write_manifest(path: Path, manifest: Mapping[str, object]) -> str:
    """Write one immutable manifest and return its file SHA256."""
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.read_bytes() != encoded:
            raise ValueError("immutable recovery manifest collision") from None
    else:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    return hashlib.sha256(encoded).hexdigest()


def cleanup_worktree(
    proof: WorkspaceProof,
    *,
    home: Path,
    repository: Path,
    execute: bool = False,
    receipt_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_counter: Callable[[Path], int] = _workspace_process_count,
) -> dict[str, object]:
    """Plan or execute repository-native removal of one exact eligible worktree.

    This is the module's own self-contained cleanup path: cleanup_decision is
    always consulted before any removal command is even planned, and no
    removal command runs unless the decision is CLEANUP_ELIGIBLE. No
    production caller invokes this with execute=True yet; see the module
    docstring and docs/fleet/workspace-lifecycle.md.
    """
    repository = repository.resolve(strict=True)
    proof_digest = recovery_manifest(proof, WorkspaceState.CLEANUP_ELIGIBLE)["content_sha256"]
    if receipt_path and receipt_path.is_file():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {
                "state": WorkspaceState.CLEANUP_FAILED.value,
                "reasons": ("invalid-receipt",),
                "executed": False,
            }
        if (
            receipt.get("proof_sha256") == proof_digest
            and receipt.get("state") == WorkspaceState.CLEANED.value
        ):
            return {**receipt, "executed": False, "retry": True}
        return {
            "state": WorkspaceState.CLEANUP_FAILED.value,
            "reasons": ("receipt-mismatch",),
            "executed": False,
        }
    # Deterministic retry with no receipt: Git is the authority on whether this
    # exact worktree is already gone. A workspace the proof already declared
    # clean (no dirty, no untracked, no unique bytes) and that Git no longer
    # registers is a prior successful removal, not a mystery; report CLEANED
    # with retry=True instead of raising or guessing at recovery. A workspace
    # missing while still registered, or one that was dirty, stays a failure.
    workspace_path = Path(proof.workspace)
    if not workspace_path.exists():
        _, live = _registered_worktrees(runner, repository)
        already_clean = proof.dirty_paths == 0 and proof.untracked_paths == 0
        if already_clean and workspace_path.resolve() not in live:
            return {
                "state": WorkspaceState.CLEANED.value,
                "executed": False,
                "retry": True,
                "bytes_reclaimed": 0,
                "recovery_status": "preserved",
                "proof_sha256": proof_digest,
                "reasons": ("workspace-already-removed",),
            }
    decision = cleanup_decision(
        proof, home=home, repository=repository, runner=runner, process_counter=process_counter
    )
    if decision.state is not WorkspaceState.CLEANUP_ELIGIBLE:
        return {"state": decision.state.value, "reasons": decision.reasons, "executed": False}
    workspace = Path(proof.workspace).resolve(strict=True)
    command: Sequence[str] = (
        "git",
        "-C",
        str(repository),
        "worktree",
        "remove",
        "--",
        str(workspace),
    )
    if not execute:
        return {
            "state": WorkspaceState.CLEANUP_ELIGIBLE.value,
            "command": list(command),
            "executed": False,
        }
    if receipt_path is None:
        return {
            "state": WorkspaceState.CLEANUP_FAILED.value,
            "reasons": ("receipt-path-required",),
            "executed": False,
        }
    if process_counter(workspace) != 0:
        return {
            "state": WorkspaceState.CLEANUP_FAILED.value,
            "reasons": ("active-processes-race",),
            "executed": False,
        }
    before = sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file())
    result = runner(command, capture_output=True, text=True, check=False)
    _, after = _registered_worktrees(runner, repository)
    exists_after = workspace.exists()
    # Verify actual post-command bytes, not just the command's word: a retained
    # workspace must still hold the bytes this module promised to preserve, and
    # a partial removal must report how much of it actually survived.
    remaining = (
        sum(path.stat().st_size for path in workspace.rglob("*") if path.is_file())
        if exists_after
        else 0
    )
    removed = not exists_after and workspace not in after
    state = (
        WorkspaceState.CLEANED
        if result.returncode == 0 and removed
        else WorkspaceState.CLEANUP_FAILED
    )
    payload: dict[str, object] = {
        "state": state.value,
        "executed": True,
        "bytes_reclaimed": before if state is WorkspaceState.CLEANED else 0,
        "bytes_remaining": remaining,
        "recovery_status": (
            "preserved"
            if state is WorkspaceState.CLEANED
            else ("workspace-retained" if exists_after else "workspace-missing")
        ),
        "command_returncode": result.returncode,
        "path_exists_after": exists_after,
        "registered_after": workspace in after,
        "proof_sha256": proof_digest,
    }
    if state is WorkspaceState.CLEANED:
        write_manifest(receipt_path, payload)
    return payload
