"""Govern preservation, quarantine, and exact fleet workspace cleanup."""

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


@dataclass(frozen=True)
class WorkspaceProof:
    """Non-secret proof for one exact card, claim, and workspace generation."""

    card_id: str
    claim_revision: str
    workspace: str
    branch: str
    head: str
    porcelain_sha256: str
    dirty_paths: int
    untracked_paths: int
    custody_kind: str | None
    custody_locator: str | None
    custody_sha256: str | None
    custody_reachable: bool
    completion_evidence_sha256: str | None
    review_required: bool
    review_dependency: str | None
    review_status: str | None
    active_processes: int
    recovery_instructions: tuple[str, ...]
    outcome: str
    completion_evidence_locator: str | None = None
    review_evidence_locator: str | None = None
    review_evidence_sha256: str | None = None


@dataclass(frozen=True)
class LifecycleDecision:
    """A deterministic state decision with fail-closed reasons."""

    state: WorkspaceState
    reasons: tuple[str, ...]


def _valid_hash(value: str | None) -> bool:
    return bool(value and _HEX.fullmatch(value.lower()))


def _file_sha256(path: Path) -> str:
    """Hash one regular evidence file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _evidence_file(locator: str | None, expected: str | None) -> bool:
    """Verify one exact regular evidence file and its caller-pinned digest."""
    if not locator or not _valid_hash(expected):
        return False
    path = Path(locator)
    try:
        return path.is_file() and _file_sha256(path) == expected
    except OSError:
        return False


def _evidence_contains(locator: str | None, *needles: str) -> bool:
    """Require identity and verdict text in one bounded evidence file."""
    if not locator:
        return False
    try:
        content = Path(locator).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return all(needle in content for needle in needles)


def _review_passes(locator: str | None, review_dependency: str) -> bool:
    """Read an exact PASS verdict bound to the expected review card."""
    if not locator:
        return False
    try:
        content = Path(locator).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return review_dependency in content and bool(
        re.search(r"(?mi)^\s*(?:verdict\s*:\s*)?PASS\s*$", content)
    )


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


def _custody_verified(
    proof: WorkspaceProof,
    repository: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    """Prove the workspace head is recoverable from independent custody."""
    if not proof.custody_locator or not _valid_hash(proof.custody_sha256):
        return False
    if proof.custody_kind in {"bundle", "artifact"}:
        custody = Path(proof.custody_locator)
        if not _evidence_file(str(custody), proof.custody_sha256):
            return False
        verify = _git(runner, repository, "bundle", "verify", str(custody))
        heads = _git(runner, repository, "bundle", "list-heads", str(custody))
        return verify.returncode == 0 and any(
            line.split(maxsplit=1)[0] == proof.head for line in heads.stdout.splitlines()
        )
    if proof.custody_kind == "commit":
        ref = proof.custody_locator
        if not ref.startswith(("refs/remotes/", "refs/tags/", "refs/custody/")):
            return False
        resolved = _git(runner, repository, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if resolved.returncode or resolved.stdout.strip() != proof.head:
            return False
        obj = _git(runner, repository, "cat-file", "commit", proof.head)
        return (
            obj.returncode == 0
            and hashlib.sha256(obj.stdout.encode()).hexdigest() == proof.custody_sha256
        )
    return False


def recovery_manifest(proof: WorkspaceProof, state: WorkspaceState) -> dict[str, object]:
    """Return a hash-pinned manifest without inspecting or changing the workspace."""
    payload: dict[str, object] = {
        "schema": "skcapstone.workspace-recovery.v1",
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
    repository: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_counter: Callable[[Path], int] = _workspace_process_count,
) -> LifecycleDecision:
    """Derive cleanup eligibility from authoritative Git, file, and process state."""
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
    if not _custody_verified(proof, repository, runner):
        reasons.append("custody-unverified")
    if not _evidence_file(
        proof.completion_evidence_locator, proof.completion_evidence_sha256
    ) or not _evidence_contains(proof.completion_evidence_locator, proof.card_id):
        reasons.append("completion-evidence")
    if not proof.recovery_instructions:
        reasons.append("recovery-instructions")
    if proof.review_required:
        if (
            not proof.review_dependency
            or proof.review_status != "PASS"
            or not _evidence_file(proof.review_evidence_locator, proof.review_evidence_sha256)
            or not _review_passes(proof.review_evidence_locator, proof.review_dependency)
        ):
            reasons.append("review-incomplete")
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
    repository: Path,
    execute: bool = False,
    receipt_path: Path | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    process_counter: Callable[[Path], int] = _workspace_process_count,
) -> dict[str, object]:
    """Plan or execute repository-native removal of one exact eligible worktree."""
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
        proof, repository=repository, runner=runner, process_counter=process_counter
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
        "recovery_status": "preserved"
        if state is WorkspaceState.CLEANED
        else ("workspace-retained" if exists_after else "workspace-missing"),
        "command_returncode": result.returncode,
        "path_exists_after": exists_after,
        "registered_after": workspace in after,
        "proof_sha256": proof_digest,
    }
    if state is WorkspaceState.CLEANED:
        write_manifest(receipt_path, payload)
    return payload
