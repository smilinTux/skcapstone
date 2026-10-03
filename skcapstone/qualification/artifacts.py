"""Governed durable sink for sealed security and review artifacts."""

from __future__ import annotations

import errno
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .jsonutil import SENSITIVE_JSON_KEYS, StrictJsonError, strict_json_loads


class ArtifactError(RuntimeError):
    """Raised when an artifact bundle is incomplete or unsafe to retain."""


@dataclass(frozen=True)
class ArtifactReceipt:
    """Durable content-addressed artifact receipt."""

    bundle_sha256: str
    acceptance_state: str
    directory: Path
    receipt_path: Path


_ALLOWED_MANIFEST_KEYS = {
    "schema",
    "sealed",
    "disposition",
    "producer",
    "accepted_source_sha256",
    "retention_policy",
    "retain_until",
    "files",
    "metadata",
}
_ALLOWED_KINDS = {
    "coverage",
    "findings",
    "inventory",
    "manifest",
    "report",
    "review",
    "threat_model",
    "validation",
}
_ALLOWED_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".sha256", ".sarif"}
_SENSITIVE = (
    re.compile(r"-----BEGIN (?:PGP |OPENSSH |RSA |EC )?PRIVATE KEY", re.IGNORECASE),
    re.compile(r"(?im)['\"]?authorization['\"]?\s*[:=]\s*['\"]?bearer\s+[^'\"\s,}]+"),
    re.compile(
        r"(?im)(?:^|[,{;\s])['\"]?(?:password|secret|api[_-]?key|access[_-]?token|"
        r"client[_-]?secret|refresh[_-]?token)['\"]?"
        r"\s*[:=]\s*(?:['\"][^'\"]+['\"]|[^\s#]+)"
    ),
)


def _normalized_key(value: object) -> str:
    """Normalize a structured key for conservative credential screening."""
    return re.sub(r"[^a-z0-9]", "", value.lower()) if isinstance(value, str) else ""


def _structured_secret(value: object, *, key: str = "") -> bool:
    """Return whether parsed JSON contains credential material at any depth."""
    normalized = _normalized_key(key)
    if normalized == "authorization" and isinstance(value, str):
        if re.match(r"(?i)^\s*bearer\s+\S+", value):
            return True
    if normalized in SENSITIVE_JSON_KEYS and value not in (None, "", "[REDACTED]"):
        return True
    if isinstance(value, dict):
        return any(_structured_secret(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, list):
        return any(_structured_secret(item, key=key) for item in value)
    return False


def _json_contains_secret(text: str, suffix: str) -> bool:
    """Strictly parse JSON or JSONL and inspect nested keys when possible."""
    try:
        if suffix == ".jsonl":
            values = [strict_json_loads(line) for line in text.splitlines() if line.strip()]
        else:
            values = [strict_json_loads(text)]
    except StrictJsonError:
        return False
    return any(_structured_secret(value) for value in values)


def _reject_symlink_chain(path: Path) -> None:
    """Reject symlinks in every existing component of a lexical path."""
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise ArtifactError(f"artifact sink path contains a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _sha256(path: Path) -> str:
    """Hash one file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(payload: dict) -> str:
    """Serialize JSON deterministically."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def _validate_manifest(manifest: dict) -> None:
    """Validate the closed durable artifact manifest contract."""
    extra = set(manifest) - _ALLOWED_MANIFEST_KEYS
    if extra:
        raise ArtifactError(f"unknown manifest fields: {', '.join(sorted(extra))}")
    required = {
        "schema",
        "sealed",
        "disposition",
        "producer",
        "accepted_source_sha256",
        "retention_policy",
        "files",
    }
    if required - set(manifest):
        raise ArtifactError("artifact manifest is incomplete")
    if manifest["schema"] != "skcapstone-review-artifact/v1":
        raise ArtifactError("unsupported artifact manifest schema")
    if not isinstance(manifest["sealed"], bool):
        raise ArtifactError("sealed must be a boolean")
    if not isinstance(manifest["disposition"], str) or manifest["disposition"] not in {
        "accepted",
        "rejected",
        "unsealed",
    }:
        raise ArtifactError("invalid artifact disposition")
    producer = manifest["producer"]
    if not isinstance(producer, dict) or set(producer) != {"name", "version"}:
        raise ArtifactError("producer must contain exact name and version fields")
    if not all(isinstance(producer[key], str) and producer[key] for key in producer):
        raise ArtifactError("producer fields must be nonempty strings")
    if not isinstance(manifest["accepted_source_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", manifest["accepted_source_sha256"]
    ):
        raise ArtifactError("accepted source digest is invalid")
    if not isinstance(manifest["retention_policy"], str) or manifest["retention_policy"] not in {
        "release",
        "review",
        "temporary",
    }:
        raise ArtifactError("invalid retention policy")
    if not isinstance(manifest["files"], list) or not manifest["files"]:
        raise ArtifactError("artifact manifest must list files")
    if len(manifest["files"]) > 1000:
        raise ArtifactError("artifact manifest exceeds the 1000-file limit")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ArtifactError("artifact metadata must be an object")
    retain_until = manifest.get("retain_until")
    if retain_until is not None and not isinstance(retain_until, str):
        raise ArtifactError("retain_until must be a string or null")


def _resolve_file(source: Path, relative: str) -> Path:
    """Resolve one regular, contained, non-symlink artifact file."""
    if (
        not relative
        or "\x00" in relative
        or "\\" in relative
        or PurePosixPath(relative).is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or PurePosixPath(relative).as_posix() != relative
    ):
        raise ArtifactError(f"artifact path must be relative: {relative!r}")
    candidate = source / relative
    for path in (candidate, *candidate.parents):
        if path == source.parent:
            break
        if path.is_symlink():
            raise ArtifactError(f"artifact symlink is forbidden: {relative}")
        if path == source:
            break
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source)
    except (FileNotFoundError, ValueError) as exc:
        raise ArtifactError(f"artifact file is missing or outside bundle: {relative}") from exc
    if not resolved.is_file():
        raise ArtifactError(f"artifact path is not a regular file: {relative}")
    if resolved.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ArtifactError(f"artifact extension is not review-safe: {relative}")
    if resolved.stat().st_size > 10 * 1024 * 1024:
        raise ArtifactError(f"artifact exceeds 10 MiB limit: {relative}")
    return resolved


def _screen_content(path: Path, relative: str) -> None:
    """Reject binary, credential, private-key, and raw-token content."""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError(f"artifact is not UTF-8 text: {relative}") from exc
    if _json_contains_secret(text, path.suffix.lower()) or any(
        pattern.search(text) for pattern in _SENSITIVE
    ):
        raise ArtifactError(f"sensitive material detected in artifact: {relative}")


def _validated_files(source: Path, manifest: dict) -> tuple[tuple[str, Path, str, str], ...]:
    """Resolve and verify every manifest-bound artifact file."""
    seen: set[str] = set()
    files: list[tuple[str, Path, str, str]] = []
    total_size = 0
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "kind"}:
            raise ArtifactError("each artifact file needs exact path, sha256, and kind")
        relative = item["path"]
        if not isinstance(relative, str):
            raise ArtifactError("artifact path must be a string")
        if relative in seen:
            raise ArtifactError(f"duplicate artifact path: {relative}")
        seen.add(relative)
        if not isinstance(item["kind"], str) or item["kind"] not in _ALLOWED_KINDS:
            raise ArtifactError(f"unsupported artifact kind: {item['kind']}")
        expected = item["sha256"]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ArtifactError(f"invalid artifact digest: {relative}")
        path = _resolve_file(source, relative)
        total_size += path.stat().st_size
        if total_size > 100 * 1024 * 1024:
            raise ArtifactError("artifact bundle exceeds the 100 MiB limit")
        actual = _sha256(path)
        if actual != expected:
            raise ArtifactError(f"artifact digest mismatch: {relative}")
        _screen_content(path, relative)
        files.append((relative, path, actual, item["kind"]))
    return tuple(sorted(files))


def _bundle_digest(manifest_bytes: bytes, files: tuple[tuple[str, Path, str, str], ...]) -> str:
    """Bind the exact manifest bytes, file paths, kinds, and digests."""
    digest = hashlib.sha256()
    digest.update(manifest_bytes)
    for relative, _path, file_sha, kind in files:
        digest.update(f"\x00{relative}\x00{kind}\x00{file_sha}".encode())
    return digest.hexdigest()


def _acceptance_state(manifest: dict) -> str:
    """Derive a state that cannot label unsealed or rejected input accepted."""
    if not manifest["sealed"]:
        return "unsealed"
    if manifest["disposition"] != "accepted":
        return manifest["disposition"]
    return "accepted"


def _receipt_payload(
    manifest: dict,
    bundle_sha: str,
    files: tuple[tuple[str, Path, str, str], ...],
) -> dict:
    """Derive the complete deterministic receipt from stored content."""
    return {
        "schema": "skcapstone-durable-artifact/v1",
        "bundle_sha256": bundle_sha,
        "acceptance_state": _acceptance_state(manifest),
        "accepted_source_sha256": manifest["accepted_source_sha256"],
        "producer": manifest["producer"],
        "retention_policy": manifest["retention_policy"],
        "retain_until": manifest.get("retain_until"),
        "files": [
            {"path": relative, "sha256": digest, "kind": kind}
            for relative, _path, digest, kind in files
        ],
    }


def _receipt_markdown(receipt: dict) -> str:
    """Render the deterministic human-readable receipt projection."""
    return (
        "# Durable review artifact\n\n"
        f"- Bundle SHA256: `{receipt['bundle_sha256']}`\n"
        f"- Acceptance state: `{receipt['acceptance_state']}`\n"
        f"- Accepted source SHA256: `{receipt['accepted_source_sha256']}`\n"
        f"- Retention policy: `{receipt['retention_policy']}`\n"
    )


def _verify_stored(target: Path, bundle_sha: str) -> ArtifactReceipt:
    """Verify an existing content-addressed bundle before returning it."""
    _reject_symlink_chain(target)
    if target.is_symlink() or not target.is_dir():
        raise ArtifactError("durable bundle target is not a regular directory")
    receipt_path = target / "receipt.json"
    manifest_path = target / "scan-manifest.json"
    projection_path = target / "receipt.md"
    if any(path.is_symlink() for path in (receipt_path, manifest_path, projection_path)):
        raise ArtifactError("stored artifact bundle contains a symlink")
    try:
        receipt = strict_json_loads(receipt_path.read_text(encoding="utf-8"))
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > 1024 * 1024:
            raise ArtifactError("artifact manifest exceeds the 1 MiB limit")
        manifest = strict_json_loads(manifest_bytes)
    except (FileNotFoundError, UnicodeDecodeError, StrictJsonError) as exc:
        raise ArtifactError("existing durable bundle is incomplete") from exc
    if not isinstance(receipt, dict) or not isinstance(manifest, dict):
        raise ArtifactError("existing durable bundle metadata is malformed")
    _validate_manifest(manifest)
    files = _validated_files(target / "files", manifest)
    if _bundle_digest(manifest_bytes, files) != bundle_sha:
        raise ArtifactError("existing durable bundle content digest disagrees")
    expected_receipt = _receipt_payload(manifest, bundle_sha, files)
    if receipt != expected_receipt:
        raise ArtifactError("stored artifact receipt disagrees with durable content")
    expected_files = {
        "scan-manifest.json",
        "receipt.json",
        "receipt.md",
        *(f"files/{relative}" for relative, _path, _digest, _kind in files),
    }
    actual_files: set[str] = set()
    expected_directories = {"files"}
    for relative in expected_files:
        parent = Path(relative).parent
        while parent != Path("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories: set[str] = set()
    for path in target.rglob("*"):
        if path.is_symlink():
            raise ArtifactError("stored artifact bundle contains a symlink")
        relative = path.relative_to(target).as_posix()
        if path.is_dir():
            actual_directories.add(relative)
        elif path.is_file():
            if path.stat().st_nlink != 1:
                raise ArtifactError("stored artifact bundle contains a linked file")
            actual_files.add(relative)
        else:
            raise ArtifactError("stored artifact bundle contains a non-regular entry")
    if actual_files != expected_files or actual_directories != expected_directories:
        raise ArtifactError("stored artifact bundle inventory disagrees with its manifest")
    try:
        receipt_markdown = projection_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ArtifactError("existing durable bundle receipt is incomplete") from exc
    if receipt_markdown != _receipt_markdown(expected_receipt):
        raise ArtifactError("stored artifact receipt projection disagrees")
    return ArtifactReceipt(bundle_sha, receipt["acceptance_state"], target, receipt_path)


def _make_read_only(root: Path) -> None:
    """Remove write permissions after the complete bundle has been verified."""
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            raise ArtifactError("refusing to chmod a symlink in durable artifact storage")
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def ingest_artifact_bundle(source_dir: Path, sink_root: Path) -> ArtifactReceipt:
    """Validate, copy, verify, and retain a review bundle by content hash."""
    source_input = Path(source_dir).expanduser().absolute()
    _reject_symlink_chain(source_input)
    source = source_input.resolve(strict=True)
    manifest_path = source / "scan-manifest.json"
    if manifest_path.is_symlink():
        raise ArtifactError("artifact manifest may not be a symlink")
    try:
        manifest_bytes = manifest_path.read_bytes()
        if len(manifest_bytes) > 1024 * 1024:
            raise ArtifactError("artifact manifest exceeds the 1 MiB limit")
        manifest = strict_json_loads(manifest_bytes)
    except (FileNotFoundError, UnicodeDecodeError, StrictJsonError) as exc:
        raise ArtifactError("artifact manifest is missing or invalid") from exc
    if not isinstance(manifest, dict):
        raise ArtifactError("artifact manifest must be an object")
    _validate_manifest(manifest)
    _screen_content(manifest_path, "scan-manifest.json")
    files = _validated_files(source, manifest)
    bundle_sha = _bundle_digest(manifest_bytes, files)
    sink = Path(sink_root).expanduser().absolute()
    _reject_symlink_chain(sink)
    sink.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(sink)
    objects = sink / "sha256"
    objects.mkdir(parents=True, exist_ok=True)
    _reject_symlink_chain(objects)
    target = objects / bundle_sha
    if target.exists():
        checked = _verify_stored(target, bundle_sha)
        _make_read_only(target)
        return checked
    staging = sink / f".staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    published = False
    try:
        file_root = staging / "files"
        file_root.mkdir()
        stored_files: list[dict] = []
        for relative, original, file_sha, kind in files:
            destination = file_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, destination)
            if _sha256(destination) != file_sha:
                raise ArtifactError(f"copied artifact digest mismatch: {relative}")
            stored_files.append({"path": relative, "sha256": file_sha, "kind": kind})
        shutil.copyfile(manifest_path, staging / "scan-manifest.json")
        receipt = _receipt_payload(manifest, bundle_sha, files)
        if receipt["files"] != stored_files:
            raise ArtifactError("copied artifact inventory disagrees with validated content")
        (staging / "receipt.json").write_text(_canonical_json(receipt), encoding="utf-8")
        (staging / "receipt.md").write_text(_receipt_markdown(receipt), encoding="utf-8")
        try:
            staging.rename(target)
            published = True
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY} or not target.exists():
                raise
            shutil.rmtree(staging)
            checked = _verify_stored(target, bundle_sha)
            _make_read_only(target)
            return checked
        checked = _verify_stored(target, bundle_sha)
        _make_read_only(target)
        return checked
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        elif published and target.exists():
            for path in sorted(target.rglob("*"), reverse=True):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o600)
            target.chmod(0o755)
            shutil.rmtree(target)
        raise
