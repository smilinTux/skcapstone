#!/usr/bin/env python3
"""Closed-manifest verification and atomic activation for the fleet launcher."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

REQUIRED_ENTRY_KEYS = {
    "path", "sha256", "size", "owner", "mode", "source_commit", "required_by"
}


class BundleError(RuntimeError):
    """The runtime bundle is incomplete or does not match its manifest."""


def _load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object, rejecting duplicate keys and trailing content."""
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise BundleError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"cannot parse manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BundleError("manifest root must be an object")
    return value


def _safe_relative(value: str) -> Path:
    """Return a normalized relative path that cannot escape a bundle root."""
    posix = PurePosixPath(value)
    if posix.is_absolute() or not posix.parts or ".." in posix.parts or "." in posix.parts:
        raise BundleError(f"unsafe relative path: {value!r}")
    return Path(*posix.parts)


def validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Validate manifest shape, fields, uniqueness, and dependency edges."""
    if manifest.get("schema_version") != 1:
        raise BundleError("schema_version must equal 1")
    release = manifest.get("release")
    if not isinstance(release, str) or not release:
        raise BundleError("release must be a non-empty string")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise BundleError("files must be a non-empty array")
    paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != REQUIRED_ENTRY_KEYS:
            raise BundleError(f"file entry must have exactly {sorted(REQUIRED_ENTRY_KEYS)}")
        path = entry["path"]
        if not isinstance(path, str):
            raise BundleError("file path must be a string")
        _safe_relative(path)
        if path in paths:
            raise BundleError(f"duplicate file path: {path}")
        paths.add(path)
        digest = entry["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BundleError(f"invalid sha256 for {path}")
        if not isinstance(entry["size"], int) or isinstance(entry["size"], bool) or entry["size"] < 0:
            raise BundleError(f"invalid size for {path}")
        if not isinstance(entry["owner"], str) or not entry["owner"]:
            raise BundleError(f"invalid owner for {path}")
        if not isinstance(entry["mode"], str) or len(entry["mode"]) != 4 or any(c not in "01234567" for c in entry["mode"]):
            raise BundleError(f"invalid mode for {path}")
        commit = entry["source_commit"]
        if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise BundleError(f"invalid source_commit for {path}")
        if not isinstance(entry["required_by"], list) or not all(isinstance(x, str) for x in entry["required_by"]):
            raise BundleError(f"invalid required_by for {path}")
    for entry in entries:
        unknown = set(entry["required_by"]) - paths
        if unknown:
            raise BundleError(f"unknown required_by edge for {entry['path']}: {sorted(unknown)}")
    return entries


def verify_payload(manifest_path: Path, payload_root: Path, check_owner: bool = True) -> dict[str, Any]:
    """Verify every payload byte and metadata item before activation."""
    manifest = _load_json(manifest_path)
    entries = validate_manifest(manifest)
    expected = {entry["path"] for entry in entries}
    actual = {
        path.relative_to(payload_root).as_posix()
        for path in payload_root.rglob("*")
        if path.is_file() and path.resolve() != manifest_path.resolve()
    }
    extras = actual - expected
    missing = expected - actual
    if missing or extras:
        raise BundleError(f"payload closure mismatch missing={sorted(missing)} extra={sorted(extras)}")
    for entry in entries:
        path = payload_root / _safe_relative(entry["path"])
        data = path.read_bytes()
        if len(data) != entry["size"]:
            raise BundleError(f"size mismatch: {entry['path']}")
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise BundleError(f"sha256 mismatch: {entry['path']}")
        if stat.S_IMODE(path.stat().st_mode) != int(entry["mode"], 8):
            raise BundleError(f"mode mismatch: {entry['path']}")
        if check_owner and pwd.getpwuid(path.stat().st_uid).pw_name != entry["owner"]:
            raise BundleError(f"owner mismatch: {entry['path']}")
    return manifest


def launcher_references(launcher: Path, repo_root: Path) -> set[str]:
    """Find the transitive repository-local imports, helpers, and schemas."""
    refs: set[str] = set()
    pending = [launcher]
    scanned: set[Path] = set()
    while pending:
        source = pending.pop()
        if source in scanned:
            continue
        scanned.add(source)
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        discovered: set[Path] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                candidate = repo_root / "src" / Path(*module.split("."))
                if candidate.with_suffix(".py").is_file():
                    discovered.add(candidate.with_suffix(".py"))
                elif (candidate / "__init__.py").is_file():
                    discovered.add(candidate / "__init__.py")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                value = node.value
                if value.endswith((".py", ".json", ".schema")):
                    candidates = (source.parent / value, repo_root / value)
                    for candidate in candidates:
                        try:
                            candidate.relative_to(repo_root)
                        except ValueError:
                            continue
                        if candidate.is_file():
                            discovered.add(candidate)
                            break
                    else:
                        # A basename next to the launcher is still a dependency
                        # when its bytes are absent, which is the outage state.
                        if "/" not in value and "\\" not in value:
                            discovered.add(source.parent / value)
        for candidate in discovered:
            refs.add(candidate.relative_to(repo_root).as_posix())
            if candidate.suffix == ".py" and candidate.is_file():
                pending.append(candidate)
    return refs


def _validate_activation_evidence(manifest: dict[str, Any]) -> None:
    """Require independently hashed review and rollout gates before activation."""
    evidence = manifest.get("activation_evidence")
    required = {"independent_review", "release", "canary", "five_host_rollout"}
    if not isinstance(evidence, dict) or set(evidence) != required:
        raise BundleError(f"activation_evidence must have exactly {sorted(required)}")
    for kind, value in evidence.items():
        if not isinstance(value, dict) or set(value) != {"artifact", "sha256"}:
            raise BundleError(f"invalid {kind} activation evidence")
        if not isinstance(value["artifact"], str) or not value["artifact"]:
            raise BundleError(f"missing {kind} evidence artifact")
        digest = value["sha256"]
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BundleError(f"invalid {kind} evidence sha256")


def verify_static_closure(manifest_path: Path, payload_root: Path, launcher_path: str) -> None:
    """Reject launcher references absent from either manifest or payload."""
    manifest = _load_json(manifest_path)
    entries = validate_manifest(manifest)
    declared = {entry["path"] for entry in entries}
    launcher = payload_root / _safe_relative(launcher_path)
    refs = launcher_references(launcher, payload_root)
    absent = refs - declared
    if absent:
        raise BundleError(f"launcher references absent from manifest: {sorted(absent)}")
    missing = {path for path in refs if not (payload_root / path).is_file()}
    if missing:
        raise BundleError(f"launcher references absent from payload: {sorted(missing)}")


def _atomic_symlink(link: Path, target: str) -> None:
    """Atomically replace a relative symlink."""
    temporary = link.with_name(f".{link.name}.{os.getpid()}.tmp")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def install(manifest_path: Path, payload_root: Path, runtime_root: Path, health_command: list[str] | None = None) -> Path:
    """Stage, verify, atomically activate, and roll back a complete release."""
    manifest = verify_payload(manifest_path, payload_root)
    _validate_activation_evidence(manifest)
    launcher = manifest.get("launcher")
    if not isinstance(launcher, str):
        raise BundleError("launcher must be a relative path")
    verify_static_closure(manifest_path, payload_root, launcher)
    release = manifest["release"]
    versions = runtime_root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    destination = versions / release
    if destination.exists():
        raise BundleError(f"release already staged: {release}")
    previous = os.readlink(runtime_root / "current") if (runtime_root / "current").is_symlink() else None
    staging = Path(tempfile.mkdtemp(prefix=f".{release}.", dir=versions))
    activated = False
    try:
        for entry in manifest["files"]:
            source = payload_root / entry["path"]
            target = staging / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, int(entry["mode"], 8))
            shutil.chown(target, user=entry["owner"])
        shutil.copyfile(manifest_path, staging / "runtime-manifest.json")
        verify_payload(staging / "runtime-manifest.json", staging)
        os.replace(staging, destination)
        _atomic_symlink(runtime_root / "current", f"versions/{release}")
        activated = True
        if health_command:
            subprocess.run(health_command, cwd=runtime_root / "current", check=True)
        return destination
    except Exception:
        if activated:
            if previous is None:
                (runtime_root / "current").unlink(missing_ok=True)
            else:
                _atomic_symlink(runtime_root / "current", previous)
        if staging.exists():
            shutil.rmtree(staging)
        if destination.exists() and activated:
            shutil.rmtree(destination)
        raise


def main(argv: list[str] | None = None) -> int:
    """Run verification or installation from the command line."""
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--health-command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        manifest = verify_payload(args.manifest, args.payload)
        verify_static_closure(args.manifest, args.payload, manifest["launcher"])
        if not args.verify_only:
            if args.runtime_root is None:
                parser.error("--runtime-root is required for installation")
            install(args.manifest, args.payload, args.runtime_root, args.health_command)
    except (BundleError, OSError, subprocess.CalledProcessError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
