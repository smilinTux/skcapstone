"""Filesystem primitives for immutable qualification checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable


class CheckpointError(RuntimeError):
    """Raised when a review checkpoint cannot remain fail closed."""


def sha256_file(path: Path) -> str:
    """Hash one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    """Hash a byte string."""
    return hashlib.sha256(value).hexdigest()


def canonical_json(payload: dict) -> str:
    """Serialize JSON deterministically."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def canonical_relative(value: str) -> str:
    """Validate one canonical POSIX path relative to a governed root."""
    if not value or "\x00" in value or "\\" in value or PurePosixPath(value).is_absolute():
        raise CheckpointError(f"path must be canonical and relative: {value!r}")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise CheckpointError(f"path must be canonical and relative: {value!r}")
    if PurePosixPath(value).as_posix() != value:
        raise CheckpointError(f"path must be canonical and relative: {value!r}")
    return value


def reject_symlink_chain(path: Path) -> None:
    """Reject symlinks in an existing lexical path chain."""
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise CheckpointError(f"symlink path is not allowed: {current}")
        if current == current.parent:
            return
        current = current.parent


def contained_regular(root: Path, relative: str) -> Path:
    """Return a contained regular file while rejecting every symlink component."""
    canonical = canonical_relative(relative)
    reject_symlink_chain(root)
    base = root.resolve(strict=True)
    candidate = base / canonical
    current = candidate
    while current != base:
        if current.is_symlink():
            raise CheckpointError(f"symlink path is not allowed: {relative}")
        current = current.parent
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(base)
    except (FileNotFoundError, ValueError) as exc:
        raise CheckpointError(f"path is missing or outside root: {relative}") from exc
    if not resolved.is_file():
        raise CheckpointError(f"path is not a regular file: {relative}")
    return resolved


def resolve_paths(workspace: Path, paths: Iterable[str]) -> tuple[tuple[str, Path], ...]:
    """Resolve unique regular files contained by a workspace."""
    root = workspace.resolve(strict=True)
    resolved: dict[str, Path] = {}
    for raw in paths:
        relative = canonical_relative(raw)
        absolute = contained_regular(root, relative)
        if relative in resolved:
            raise CheckpointError(f"duplicate review path: {relative}")
        resolved[relative] = absolute
    if not resolved:
        raise CheckpointError("checkpoint requires at least one file")
    return tuple(sorted(resolved.items()))


def inventory_bytes(entries: dict[str, str]) -> bytes:
    """Build an inventory from captured path digests without re-reading files."""
    return "".join(f"{entries[path]}  {path}\n" for path in sorted(entries)).encode()


def write_read_only_new(path: Path, value: str) -> None:
    """Create one new UTF-8 file without following links, then seal its mode."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    path.chmod(0o444)
