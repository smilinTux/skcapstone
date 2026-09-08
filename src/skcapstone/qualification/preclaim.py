"""Fail-closed admission for work packets before a fleet slot is claimed.

This module is deliberately side-effect free: it only reads the packet and the
referenced immutable files.  Callers must run it before recording a claim.
"""
from __future__ import annotations

import hashlib
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class AdmissionReason:
    EMPTY = "empty"
    WRONG_REPOSITORY = "wrong_repository"
    MISSING_CANDIDATE = "missing_candidate"
    MISSING_EVIDENCE = "missing_evidence"
    MISSING_DEPENDENCY = "missing_dependency"
    UNVERIFIABLE_COMMAND = "unverifiable_command"
    INVALID_PACKET = "invalid_packet"


@dataclass(frozen=True)
class AdmissionResult:
    admitted: bool
    reason: str | None = None
    source_sha256: str | None = None
    evidence_sha256: str | None = None
    packet: Mapping[str, Any] | None = None


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reject(reason: str) -> AdmissionResult:
    return AdmissionResult(False, reason)


def admit_work_packet(packet: Mapping[str, Any] | None, *, repository: str | None = None,
                      dependencies: Mapping[str, bool] | None = None) -> AdmissionResult:
    """Validate a packet without changing it or consuming a worker slot.

    Required packet fields are intentionally explicit.  Candidate and evidence
    hashes are checked against bytes on disk and returned unchanged. Commands
    must be exact argv strings, not shell snippets, and every command executable
    must be available locally (or be an absolute path that exists).
    """
    if not isinstance(packet, Mapping) or not packet:
        return _reject(AdmissionReason.EMPTY)
    expected_repo = packet.get("repository")
    if not isinstance(expected_repo, str) or not expected_repo.strip() or (
        repository is not None and expected_repo != repository
    ):
        return _reject(AdmissionReason.WRONG_REPOSITORY)

    def artifact(key: str, missing: str) -> tuple[str | None, AdmissionResult | None]:
        value = packet.get(key)
        if not isinstance(value, Mapping):
            return None, _reject(missing)
        path = value.get("path")
        claimed = value.get("sha256")
        if not isinstance(path, str) or not path or not isinstance(claimed, str):
            return None, _reject(missing)
        candidate = Path(path)
        if not candidate.is_file():
            return None, _reject(missing)
        try:
            actual = _sha(candidate)
        except OSError:
            return None, _reject(missing)
        if actual != claimed.lower():
            return None, _reject(missing)
        return actual, None

    source, error = artifact("candidate", AdmissionReason.MISSING_CANDIDATE)
    if error:
        return error
    evidence, error = artifact("evidence", AdmissionReason.MISSING_EVIDENCE)
    if error:
        return error

    deps = packet.get("dependencies")
    if not isinstance(deps, (list, tuple)) or any(
        not isinstance(dep, str) or not dep for dep in deps
    ):
        return _reject(AdmissionReason.MISSING_DEPENDENCY)
    if dependencies is not None and any(not dependencies.get(dep, False) for dep in deps):
        return _reject(AdmissionReason.MISSING_DEPENDENCY)

    commands = packet.get("commands")
    if not isinstance(commands, (list, tuple)) or not commands:
        return _reject(AdmissionReason.UNVERIFIABLE_COMMAND)
    for command in commands:
        if (
            not isinstance(command, str)
            or not command.strip()
            or any(c in command for c in (";", "&&", "||", "|"))
        ):
            return _reject(AdmissionReason.UNVERIFIABLE_COMMAND)
        try:
            argv = shlex.split(command)
        except ValueError:
            return _reject(AdmissionReason.UNVERIFIABLE_COMMAND)
        if not argv or argv[0].startswith("-"):
            return _reject(AdmissionReason.UNVERIFIABLE_COMMAND)
        executable = Path(argv[0])
        if (executable.is_absolute() and not executable.is_file()) or (
            not executable.is_absolute() and shutil.which(argv[0]) is None
        ):
            return _reject(AdmissionReason.UNVERIFIABLE_COMMAND)

    required = ("expected_manifest", "prohibited_effects", "rollback", "workspace")
    if any(key not in packet for key in required) or not isinstance(
        packet["expected_manifest"], Mapping
    ):
        return _reject(AdmissionReason.INVALID_PACKET)
    if (
        not isinstance(packet["prohibited_effects"], (list, tuple))
        or not packet["prohibited_effects"]
    ):
        return _reject(AdmissionReason.INVALID_PACKET)
    if not isinstance(packet["rollback"], str) or not packet["rollback"].strip():
        return _reject(AdmissionReason.INVALID_PACKET)
    if packet["workspace"] not in {"read-only", "writable"}:
        return _reject(AdmissionReason.INVALID_PACKET)
    return AdmissionResult(True, source_sha256=source, evidence_sha256=evidence, packet=packet)


def admit_task(board: Any, task_id: str) -> AdmissionResult:
    """Admit a board task packet immediately before a claim mutation."""
    views = {view.task.id: view for view in board.get_task_views(include_archived=True)}
    view = views.get(task_id)
    packet = view.task.meta.get("work_packet") if view is not None else None
    dependencies = {
        dependency: views.get(dependency) is not None
        and views[dependency].status.value == "done"
        for dependency in (view.task.dependencies if view is not None else [])
    }
    return admit_work_packet(packet, repository="skcapstone", dependencies=dependencies)


def require_task_admission(board: Any, task_id: str) -> AdmissionResult:
    """Raise before claim mutation when the task packet is not admissible."""
    result = admit_task(board, task_id)
    if not result.admitted:
        raise ValueError(f"work packet rejected: {result.reason}")
    return result


preclaim_admission = admit_work_packet
