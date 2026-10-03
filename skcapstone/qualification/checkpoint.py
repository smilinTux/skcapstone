"""Immutable source checkpoints and evidence-only completion sealing."""

from __future__ import annotations

import errno
import re
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from . import _checkpoint_io
from ._checkpoint_io import CheckpointError
from .jsonutil import StrictJsonError, strict_json_loads

_canonical_json = _checkpoint_io.canonical_json
_canonical_relative = _checkpoint_io.canonical_relative
_contained_regular = _checkpoint_io.contained_regular
_inventory_digest_bytes = _checkpoint_io.inventory_bytes
_reject_symlink_chain = _checkpoint_io.reject_symlink_chain
_resolve_paths = _checkpoint_io.resolve_paths
_sha256_bytes = _checkpoint_io.sha256_bytes
_sha256 = _checkpoint_io.sha256_file
_write_read_only_new = _checkpoint_io.write_read_only_new


@dataclass(frozen=True)
class Checkpoint:
    """A created immutable source checkpoint."""

    directory: Path
    inventory_path: Path
    inventory_sha256: str


@dataclass(frozen=True)
class VerificationReceipt:
    """Result of checking a checkpoint against its snapshot and workspace."""

    sealed: bool
    inventory_sha256: str
    changed_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    snapshot_errors: tuple[str, ...]


@dataclass(frozen=True)
class ReviewReceipt:
    """One independent reviewer disposition bound to an inventory."""

    reviewer: str
    disposition: str
    inventory_sha256: str
    receipt_path: Path


@dataclass(frozen=True)
class CompletionReceipt:
    """Accepted-to-completion diff and completion inventory."""

    accepted_inventory_sha256: str
    completion_inventory_sha256: str
    changed_paths: tuple[str, ...]
    inventory_path: Path
    receipt_path: Path


def _safe_name(value: str) -> str:
    """Return a stable filename component."""
    result = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-.")
    if not result:
        raise CheckpointError("reviewer identity is empty after normalization")
    return result


def _load_inventory(checkpoint_dir: Path) -> tuple[str, dict[str, str]]:
    """Load and validate a checkpoint inventory."""
    path = checkpoint_dir / "inventory.sha256"
    if path.is_symlink():
        raise CheckpointError("checkpoint inventory may not be a symlink")
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise CheckpointError("checkpoint inventory is missing") from exc
    entries: dict[str, str] = {}
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise CheckpointError("checkpoint inventory is not UTF-8") from exc
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00]+)", line)
        if match is None:
            raise CheckpointError("checkpoint inventory is malformed")
        digest, relative = match.groups()
        relative = _canonical_relative(relative)
        if relative in entries:
            raise CheckpointError(f"duplicate inventory path: {relative}")
        entries[relative] = digest
    if not entries:
        raise CheckpointError("checkpoint inventory is empty")
    manifest_path = checkpoint_dir / "checkpoint.json"
    if manifest_path.is_symlink():
        raise CheckpointError("checkpoint manifest may not be a symlink")
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeDecodeError, StrictJsonError) as exc:
        raise CheckpointError("checkpoint manifest is missing or invalid") from exc
    expected_keys = {
        "schema",
        "created_at",
        "file_count",
        "inventory_sha256",
        "snapshot",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise CheckpointError("checkpoint manifest has unknown or missing fields")
    if manifest.get("schema") != "skcapstone-review-checkpoint/v1":
        raise CheckpointError("checkpoint manifest schema is unsupported")
    if manifest.get("file_count") != len(entries) or manifest.get("snapshot") != "source":
        raise CheckpointError("checkpoint manifest does not describe its snapshot")
    actual = _sha256_bytes(raw)
    if manifest.get("inventory_sha256") != actual:
        raise CheckpointError("checkpoint manifest does not bind the inventory")
    return actual, entries


def create_checkpoint(
    workspace: Path,
    paths: Iterable[str],
    output_dir: Path,
    *,
    created_at: str | None = None,
) -> Checkpoint:
    """Create a deterministic inventory and read-only exact source snapshot."""
    workspace_input = Path(workspace).expanduser().absolute()
    _reject_symlink_chain(workspace_input)
    root = workspace_input.resolve(strict=True)
    output = Path(output_dir).expanduser().absolute()
    _reject_symlink_chain(output)
    if output.exists():
        raise CheckpointError("checkpoint output path already exists")
    entries = _resolve_paths(root, paths)
    output.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(output.parent)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        source = staging / "source"
        source.mkdir()
        expected = {relative: _sha256(original) for relative, original in entries}
        inventory = _inventory_digest_bytes(expected)
        inventory_path = staging / "inventory.sha256"
        inventory_path.write_bytes(inventory)
        inventory_sha = _sha256_bytes(inventory)
        for relative, original in entries:
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if _sha256(target) != expected[relative]:
                raise CheckpointError(f"source changed while snapshotting: {relative}")
            target.chmod(0o444)
        stamp = created_at or datetime.now(timezone.utc).isoformat()
        manifest = {
            "schema": "skcapstone-review-checkpoint/v1",
            "created_at": stamp,
            "file_count": len(entries),
            "inventory_sha256": inventory_sha,
            "snapshot": "source",
        }
        manifest_path = staging / "checkpoint.json"
        manifest_path.write_text(_canonical_json(manifest), encoding="utf-8")
        inventory_path.chmod(0o444)
        manifest_path.chmod(0o444)
        for directory in sorted(source.rglob("*"), reverse=True):
            if directory.is_dir():
                directory.chmod(0o555)
        source.chmod(0o555)
        staging.rename(output)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return Checkpoint(output, output / "inventory.sha256", inventory_sha)


def verify_checkpoint(checkpoint_dir: Path, workspace: Path) -> VerificationReceipt:
    """Verify the immutable snapshot and current workspace without writing."""
    checkpoint_input = Path(checkpoint_dir).expanduser().absolute()
    workspace_input = Path(workspace).expanduser().absolute()
    _reject_symlink_chain(checkpoint_input)
    _reject_symlink_chain(workspace_input)
    checkpoint = checkpoint_input.resolve(strict=True)
    root = workspace_input.resolve(strict=True)
    inventory_sha, entries = _load_inventory(checkpoint)
    changed: list[str] = []
    missing: list[str] = []
    snapshot_errors: list[str] = []
    for relative, expected in sorted(entries.items()):
        try:
            snapshot = _contained_regular(checkpoint / "source", relative)
        except CheckpointError:
            snapshot_errors.append(relative)
        else:
            if _sha256(snapshot) != expected:
                snapshot_errors.append(relative)
        try:
            current = _contained_regular(root, relative)
        except CheckpointError:
            missing.append(relative)
        else:
            if _sha256(current) != expected:
                changed.append(relative)
    return VerificationReceipt(
        sealed=not (changed or missing or snapshot_errors),
        inventory_sha256=inventory_sha,
        changed_paths=tuple(changed),
        missing_paths=tuple(missing),
        snapshot_errors=tuple(snapshot_errors),
    )


def _review_markdown(payload: dict) -> str:
    """Render the exact human-readable projection of a review receipt."""
    return (
        f"# Review disposition: {payload['reviewer']}\n\n"
        f"- Disposition: `{payload['disposition']}`\n"
        f"- Inventory SHA256: `{payload['inventory_sha256']}`\n"
        f"- Reviewed at: `{payload['reviewed_at']}`\n"
    )


def record_review(
    checkpoint_dir: Path,
    workspace: Path,
    *,
    reviewer: str,
    disposition: str,
    notes: str = "",
    reviewed_at: str | None = None,
) -> ReviewReceipt:
    """Record one independent disposition after exact before-hash verification."""
    if disposition not in {"accept", "reject"}:
        raise CheckpointError("review disposition must be accept or reject")
    verification = verify_checkpoint(checkpoint_dir, workspace)
    if not verification.sealed:
        raise CheckpointError("source drift prevents a sealed review")
    checkpoint_input = Path(checkpoint_dir).expanduser().absolute()
    _reject_symlink_chain(checkpoint_input)
    checkpoint = checkpoint_input.resolve(strict=True)
    reviews = checkpoint / "reviews"
    reviews.mkdir(exist_ok=True)
    if reviews.is_symlink() or not reviews.is_dir():
        raise CheckpointError("review receipt directory must be a regular directory")
    name = _safe_name(reviewer)
    payload = {
        "schema": "skcapstone-review-disposition/v1",
        "reviewer": reviewer,
        "disposition": disposition,
        "reviewed_at": reviewed_at or datetime.now(timezone.utc).isoformat(),
        "inventory_sha256": verification.inventory_sha256,
        "notes": notes,
    }
    target = reviews / name
    rendered = _canonical_json(payload)
    markdown = _review_markdown(payload)

    def existing_receipt() -> ReviewReceipt:
        if target.is_symlink() or not target.is_dir():
            raise CheckpointError(f"review receipt path is unsafe for {reviewer}")
        entries = sorted(target.iterdir())
        if {path.name for path in entries} != {"receipt.json", "receipt.md"} or any(
            path.is_symlink() or not path.is_file() for path in entries
        ):
            raise CheckpointError(f"review receipt is incomplete for {reviewer}")
        receipt_path = target / "receipt.json"
        markdown_path = target / "receipt.md"
        try:
            existing_json = receipt_path.read_text(encoding="utf-8")
            existing_markdown = markdown_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointError(f"review receipt is incomplete for {reviewer}") from exc
        if existing_json != rendered or existing_markdown != markdown:
            raise CheckpointError(f"review receipt already exists for {reviewer}")
        return ReviewReceipt(reviewer, disposition, verification.inventory_sha256, receipt_path)

    if target.exists() or target.is_symlink():
        return existing_receipt()
    staging = reviews / f".{name}.staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        _write_read_only_new(staging / "receipt.md", markdown)
        _write_read_only_new(staging / "receipt.json", rendered)
        staging.chmod(0o555)
        staging.rename(target)
    except OSError as exc:
        if staging.exists():
            staging.chmod(0o755)
            for path in staging.iterdir():
                if not path.is_symlink():
                    path.chmod(0o600)
            shutil.rmtree(staging)
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} and (
            target.exists() or target.is_symlink()
        ):
            return existing_receipt()
        raise
    except Exception:
        if staging.exists():
            staging.chmod(0o755)
            for path in staging.iterdir():
                if not path.is_symlink():
                    path.chmod(0o600)
            shutil.rmtree(staging)
        raise
    return ReviewReceipt(
        reviewer,
        disposition,
        verification.inventory_sha256,
        target / "receipt.json",
    )


def _accepted_reviews(checkpoint: Path, inventory_sha: str) -> tuple[list[dict], dict[str, str]]:
    """Load valid review receipts and require every disposition to accept."""
    review_dir = checkpoint / "reviews"
    if review_dir.is_symlink() or not review_dir.is_dir():
        raise CheckpointError("review receipt directory is missing or invalid")
    entries = sorted(review_dir.iterdir())
    if any(path.is_symlink() or not path.is_dir() for path in entries):
        raise CheckpointError("review receipt directory contains an unsafe entry")
    receipts: list[dict] = []
    receipt_digests: dict[str, str] = {}
    reviewers: set[str] = set()
    for directory in entries:
        children = sorted(directory.iterdir())
        if {path.name for path in children} != {"receipt.json", "receipt.md"} or any(
            path.is_symlink() or not path.is_file() for path in children
        ):
            raise CheckpointError("review receipt directory contains an unpaired entry")
        path = directory / "receipt.json"
        try:
            raw = path.read_bytes()
            payload = strict_json_loads(raw)
        except (UnicodeDecodeError, StrictJsonError) as exc:
            raise CheckpointError(f"review receipt is invalid: {path.name}") from exc
        expected_keys = {
            "schema",
            "reviewer",
            "disposition",
            "reviewed_at",
            "inventory_sha256",
            "notes",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise CheckpointError(f"review receipt is incomplete: {path.name}")
        if payload.get("schema") != "skcapstone-review-disposition/v1":
            raise CheckpointError(f"review receipt schema is unsupported: {path.name}")
        if not isinstance(payload.get("reviewer"), str) or not payload["reviewer"]:
            raise CheckpointError(f"reviewer identity is invalid: {path.name}")
        if payload["reviewer"] in reviewers:
            raise CheckpointError(f"duplicate reviewer identity: {payload['reviewer']}")
        reviewers.add(payload["reviewer"])
        if payload.get("disposition") not in {"accept", "reject"}:
            raise CheckpointError(f"review disposition is invalid: {path.name}")
        if not isinstance(payload.get("reviewed_at"), str) or not isinstance(
            payload.get("notes"), str
        ):
            raise CheckpointError(f"review receipt text fields are invalid: {path.name}")
        if payload.get("inventory_sha256") != inventory_sha:
            raise CheckpointError(f"review receipt is bound to another inventory: {path.name}")
        markdown_path = directory / "receipt.md"
        try:
            markdown = markdown_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise CheckpointError(f"review receipt projection is invalid: {path.name}") from exc
        if markdown != _review_markdown(payload):
            raise CheckpointError(f"review receipt projection disagrees: {path.name}")
        receipts.append(payload)
        receipt_digests[f"{directory.name}/receipt.json"] = _sha256_bytes(raw)
    if not receipts or any(item.get("disposition") != "accept" for item in receipts):
        raise CheckpointError("completion requires accepted review receipts")
    return receipts, receipt_digests


def seal_completion(
    checkpoint_dir: Path,
    workspace: Path,
    *,
    evidence_allowlist: set[str],
) -> CompletionReceipt:
    """Seal current bytes while permitting only declared evidence changes."""
    reserved = {"completion-inventory.sha256", "completion-receipt.json"}
    if evidence_allowlist & reserved:
        raise CheckpointError("self-referential completion path is forbidden")
    evidence_paths = {_canonical_relative(path) for path in evidence_allowlist}
    checkpoint_input = Path(checkpoint_dir).expanduser().absolute()
    workspace_input = Path(workspace).expanduser().absolute()
    _reject_symlink_chain(checkpoint_input)
    _reject_symlink_chain(workspace_input)
    checkpoint = checkpoint_input.resolve(strict=True)
    root = workspace_input.resolve(strict=True)
    inventory_sha, accepted = _load_inventory(checkpoint)
    if evidence_paths & set(accepted):
        overlap = ", ".join(sorted(evidence_paths & set(accepted)))
        raise CheckpointError(f"accepted source cannot be reclassified as evidence: {overlap}")
    reviews, review_receipts = _accepted_reviews(checkpoint, inventory_sha)
    for relative, expected in accepted.items():
        try:
            snapshot = _contained_regular(checkpoint / "source", relative)
        except CheckpointError as exc:
            raise CheckpointError(f"accepted snapshot changed: {relative}") from exc
        if _sha256(snapshot) != expected:
            raise CheckpointError(f"accepted snapshot changed: {relative}")
    current_paths = set(accepted) | evidence_paths
    resolved = dict(_resolve_paths(root, current_paths))
    source_changes: list[str] = []
    evidence_changes: list[str] = []
    current_digests = {relative: _sha256(path) for relative, path in resolved.items()}
    for relative in sorted(current_paths):
        current_sha = current_digests[relative]
        if accepted.get(relative) == current_sha:
            continue
        if relative in evidence_paths:
            evidence_changes.append(relative)
        else:
            source_changes.append(relative)
    if source_changes:
        raise CheckpointError(f"source drift after acceptance: {', '.join(source_changes)}")
    completion_bytes = _inventory_digest_bytes(current_digests)
    completion_dir = checkpoint / "completion"
    if completion_dir.exists() or completion_dir.is_symlink():
        raise CheckpointError("completion has already been sealed") from FileExistsError(
            str(completion_dir)
        )
    staging = checkpoint / f".completion-staging-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        completion_inventory = staging / "inventory.sha256"
        completion_inventory.write_bytes(completion_bytes)
        completion_sha = _sha256_bytes(completion_bytes)
        receipt = {
            "schema": "skcapstone-review-completion/v1",
            "accepted_inventory_sha256": inventory_sha,
            "completion_inventory_sha256": completion_sha,
            "source_changes": source_changes,
            "evidence_changes": evidence_changes,
            "reviewers": [item["reviewer"] for item in reviews],
            "review_receipts": review_receipts,
        }
        receipt_path = staging / "receipt.json"
        receipt_path.write_text(_canonical_json(receipt), encoding="utf-8")
        receipt_markdown = staging / "receipt.md"
        receipt_markdown.write_text(
            "# Review completion receipt\n\n"
            f"- Accepted inventory SHA256: `{inventory_sha}`\n"
            f"- Completion inventory SHA256: `{completion_sha}`\n"
            f"- Evidence changes: `{', '.join(evidence_changes) or 'none'}`\n",
            encoding="utf-8",
        )
        for path in (completion_inventory, receipt_path, receipt_markdown):
            path.chmod(0o444)
        staging.chmod(0o555)
        try:
            staging.rename(completion_dir)
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.ENOTEMPTY} and (
                completion_dir.exists() or completion_dir.is_symlink()
            ):
                raise CheckpointError("completion has already been sealed") from None
            raise
    except Exception:
        if staging.exists():
            staging.chmod(0o755)
            for path in staging.iterdir():
                if not path.is_symlink():
                    path.chmod(0o600)
            shutil.rmtree(staging)
        raise
    return CompletionReceipt(
        inventory_sha,
        completion_sha,
        tuple(evidence_changes),
        completion_dir / "inventory.sha256",
        completion_dir / "receipt.json",
    )
