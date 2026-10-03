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

review_required is dropped rather than hardcoded True: Plan B1 made review
opt-in (a PR is required only for a sensitive card, decided by
scripts/fleet/skfleet-rotate.py's pr_required()), not a default every
worker's proof must carry. Whether a card needed a PR is a publishing
decision made on the pushed branch; it has nothing to do with whether this
workspace is safe to delete, so it does not belong in this module at all.

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
    RECOVERABLE_QUARANTINE = "RECOVERABLE_QUARANTINE"
    CLEANUP_ELIGIBLE = "CLEANUP_ELIGIBLE"
    CLEANED = "CLEANED"
    CLEANUP_FAILED = "CLEANUP_FAILED"


TRANSITIONS = {
    WorkspaceState.ACTIVE: {
        WorkspaceState.HANDOFF_REQUIRED,
        WorkspaceState.RECOVERABLE_QUARANTINE,
    },
    WorkspaceState.HANDOFF_REQUIRED: {
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


def recovery_manifest(proof: WorkspaceProof, state: WorkspaceState) -> dict[str, object]:
    """Return a hash-pinned manifest without inspecting or changing the workspace."""
    payload: dict[str, object] = {
        "schema": "skcapstone.workspace-recovery.v2",
        "state": state.value,
        "proof": asdict(proof),
    }
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
