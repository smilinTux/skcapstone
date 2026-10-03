"""Fail-closed vulnerability audit split for exact Git-pinned dependencies."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit

import tomllib

from .jsonutil import SENSITIVE_JSON_KEYS


class VcsAuditError(RuntimeError):
    """Raised when an exact VCS audit boundary cannot be proven."""


@dataclass(frozen=True)
class VcsDependencyPolicy:
    """Allowlisted immutable VCS dependency identity."""

    name: str
    canonical_url: str
    commit: str
    version: str

    def __post_init__(self) -> None:
        """Validate immutable policy fields."""
        if not all(
            isinstance(value, str)
            for value in (self.name, self.canonical_url, self.commit, self.version)
        ):
            raise VcsAuditError("VCS policy fields must be strings")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", self.name):
            raise VcsAuditError(f"invalid package name: {self.name}")
        if not self.canonical_url.startswith("https://") or not self.canonical_url.endswith(
            ".git"
        ):
            raise VcsAuditError("canonical VCS URL must be an HTTPS .git URL")
        parsed = urlsplit(self.canonical_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise VcsAuditError("canonical VCS URL must not contain credentials or modifiers")
        if not re.fullmatch(r"[0-9a-f]{40}", self.commit):
            raise VcsAuditError("VCS commit must be a full 40-character lowercase SHA")
        if not self.version:
            raise VcsAuditError("VCS package version must be nonempty")

    @property
    def requirement(self) -> str:
        """Return the only accepted exported requirement line."""
        return f"{self.name} @ git+{self.canonical_url}@{self.commit}"


@dataclass(frozen=True)
class VcsAuditPlan:
    """Verified requirements split ready for pip-audit."""

    output_dir: Path
    registry_requirements: Path
    vcs_release_requirements: tuple[Path, ...]
    policies: tuple[VcsDependencyPolicy, ...]
    registry_sha256: str
    release_sha256: tuple[str, ...]
    plan_sha256: str


@dataclass(frozen=True)
class VcsAuditReceipt:
    """Reconciled result of registry and VCS release queries."""

    passed: bool
    receipt_path: Path


_OUTPUT_SECRET_PATTERNS = (
    re.compile(r"(?im)(['\"]?authorization['\"]?\s*[:=]\s*['\"]?bearer\s+)" r"[^'\"\s,}]+"),
    re.compile(r"(?i)(https://)[^/@\s:]+:[^/@\s]+@"),
    re.compile(
        r"(?im)(['\"]?(?:password|secret|api[_-]?key|access[_-]?token|"
        r"client[_-]?secret|refresh[_-]?token)['\"]?\s*[:=]\s*)"
        r"(?:['\"][^'\"]+['\"]|[^\s]+)"
    ),
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_bound(path: Path, expected: str) -> bytes:
    """Read one regular plan file once and verify its captured digest."""
    if path.is_symlink() or not path.is_file():
        raise VcsAuditError(f"VCS audit plan file is missing or unsafe: {path.name}")
    value = path.read_bytes()
    if _sha256_bytes(value) != expected:
        raise VcsAuditError(f"VCS audit plan changed after validation: {path.name}")
    return value


def _write_receipt(path: Path, value: str) -> None:
    """Create one audit receipt without following or replacing an existing path."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o444)
    except OSError as exc:
        raise VcsAuditError("VCS audit receipt path already exists or is unsafe") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _sanitize_output(value: object) -> str:
    """Bound and redact subprocess output before persisting it."""
    rendered = str(value)
    try:
        structured = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        structured = None
    if structured is not None:
        rendered = json.dumps(_redact_structured_output(structured), sort_keys=True)
    for pattern in _OUTPUT_SECRET_PATTERNS:
        rendered = pattern.sub(r"\1[REDACTED]", rendered)
    if len(rendered) > 1024 * 1024:
        rendered = rendered[: 1024 * 1024] + "\n[TRUNCATED]"
    return rendered


def _redact_structured_output(value: object, *, key: str = "") -> object:
    """Redact nested credential fields in JSON subprocess output."""
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if normalized == "authorization" and isinstance(value, str):
        if re.match(r"(?i)^\s*bearer\s+\S+", value):
            return "Bearer [REDACTED]"
    if normalized in SENSITIVE_JSON_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            item_key: _redact_structured_output(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_structured_output(item, key=key) for item in value]
    return value


def _reject_output_symlinks(path: Path) -> None:
    """Reject symlinks in the lexical audit output path."""
    current = path.absolute()
    while True:
        if current.is_symlink():
            raise VcsAuditError(f"VCS audit output path contains a symlink: {current}")
        if current == current.parent:
            return
        current = current.parent


def _logical_blocks(text: str) -> list[list[str]]:
    """Group requirements continuations without reformatting their bytes."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        current.append(line)
        if line.rstrip("\r\n").rstrip().endswith("\\"):
            continue
        blocks.append(current)
        current = []
    if current:
        blocks.append(current)
    return blocks


def _requirement_name(block: list[str]) -> str | None:
    """Extract a normalized package name from one requirement block."""
    first = block[0].strip()
    if not first or first.startswith(("#", "--")):
        return None
    match = re.match(r"([A-Za-z0-9_.-]+)(?:==|\s+@\s+)", first)
    if match is None:
        return None
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _is_vcs(block: list[str]) -> bool:
    """Return whether a requirement block references any VCS transport."""
    return any("git+" in line for line in block)


def _validate_registry_hashes(blocks: Iterable[list[str]]) -> None:
    """Require a SHA256 hash on every remaining registry requirement."""
    for block in blocks:
        if _requirement_name(block) is None:
            continue
        rendered = "".join(block)
        if "--hash=sha256:" not in rendered:
            raise VcsAuditError(f"registry requirement is not hash-required: {block[0].strip()}")


def _lock_packages(lock_path: Path) -> list[dict]:
    """Load uv lock package records."""
    try:
        payload = tomllib.loads(Path(lock_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, tomllib.TOMLDecodeError) as exc:
        raise VcsAuditError("lock file is missing or invalid") from exc
    packages = payload.get("package")
    if not isinstance(packages, list) or not all(isinstance(item, dict) for item in packages):
        raise VcsAuditError("lock file has no package records")
    return packages


def _normalize_name(value: str) -> str:
    """Normalize a Python distribution name."""
    return re.sub(r"[-_.]+", "-", value).lower()


def _validate_lock(
    packages: list[dict],
    policies: tuple[VcsDependencyPolicy, ...],
    requirement_names: set[str],
) -> None:
    """Bind allowlisted VCS identities and their dependency closure to uv.lock."""
    by_name: dict[str, list[dict]] = {}
    for package in packages:
        name = package.get("name")
        if not isinstance(name, str):
            raise VcsAuditError("lock package name is invalid")
        if not isinstance(package.get("source"), dict):
            raise VcsAuditError(f"lock source is invalid for {name}")
        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise VcsAuditError(f"lock dependencies are invalid for {name}")
        for dependency in dependencies:
            dep_name = dependency.get("name") if isinstance(dependency, dict) else None
            if not isinstance(dep_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", dep_name):
                raise VcsAuditError(f"lock dependency record is invalid for {name}")
        by_name.setdefault(_normalize_name(name), []).append(package)
    policy_names = {_normalize_name(item.name) for item in policies}
    lock_vcs = {
        name
        for name, records in by_name.items()
        if any(
            isinstance(record.get("source"), dict) and "git" in record["source"]
            for record in records
        )
    }
    if lock_vcs != policy_names:
        raise VcsAuditError("lock VCS package set differs from the allowlist")
    for policy in policies:
        name = _normalize_name(policy.name)
        records = by_name.get(name, [])
        if len(records) != 1:
            raise VcsAuditError(f"lock package is missing or ambiguous: {policy.name}")
        package = records[0]
        if package.get("version") != policy.version:
            raise VcsAuditError(f"lock version drift for {policy.name}")
        source_record = package.get("source")
        if not isinstance(source_record, dict):
            raise VcsAuditError(f"lock source is invalid for {policy.name}")
        source = source_record.get("git")
        expected = f"{policy.canonical_url}?rev={policy.commit}#{policy.commit}"
        if source != expected:
            raise VcsAuditError(f"lock source drift for {policy.name}")
        dependencies = package.get("dependencies", [])
        if not isinstance(dependencies, list):
            raise VcsAuditError(f"lock dependencies are invalid for {policy.name}")
        for dependency in dependencies:
            dep_name = dependency["name"]
            if _normalize_name(dep_name) not in requirement_names:
                raise VcsAuditError(
                    f"dependency omission for {policy.name}: {_normalize_name(dep_name)}"
                )


def _installed_version(name: str) -> str:
    """Return an installed distribution version with a stable error."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError as exc:
        raise VcsAuditError(f"VCS package is not installed: {name}") from exc


def prepare_vcs_audit(
    requirements_path: Path,
    lock_path: Path,
    policies: Iterable[VcsDependencyPolicy],
    output_dir: Path,
    *,
    installed_versions: dict[str, str] | None = None,
) -> VcsAuditPlan:
    """Validate exact VCS identities and prepare a minimal audit split."""
    policy_items = tuple(sorted(policies, key=lambda item: _normalize_name(item.name)))
    normalized = [_normalize_name(item.name) for item in policy_items]
    if len(normalized) != len(set(normalized)):
        raise VcsAuditError("duplicate VCS policy package")
    try:
        text = Path(requirements_path).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise VcsAuditError("requirements export is missing") from exc
    blocks = _logical_blocks(text)
    vcs_blocks = [block for block in blocks if _is_vcs(block)]
    expected_lines = {item.requirement: item for item in policy_items}
    actual_lines: list[str] = []
    for block in vcs_blocks:
        if len(block) != 1:
            raise VcsAuditError("VCS requirement must be one exact standalone line")
        actual_lines.append(block[0].rstrip("\r\n"))
    if len(actual_lines) != len(set(actual_lines)) or set(actual_lines) != set(expected_lines):
        raise VcsAuditError("VCS requirement set is missing, ambiguous, or unapproved")
    registry_blocks = [block for block in blocks if not _is_vcs(block)]
    _validate_registry_hashes(registry_blocks)
    registry_names = {
        name for block in registry_blocks if (name := _requirement_name(block)) is not None
    }
    if registry_names & set(normalized):
        raise VcsAuditError("VCS dependency is duplicated as a registry requirement")
    requirement_names = {
        name for block in blocks if (name := _requirement_name(block)) is not None
    }
    packages = _lock_packages(Path(lock_path))
    _validate_lock(packages, policy_items, requirement_names)
    versions = installed_versions or {}
    for policy in policy_items:
        found = (
            versions[policy.name] if policy.name in versions else _installed_version(policy.name)
        )
        if found != policy.version:
            raise VcsAuditError(
                f"installed version drift for {policy.name}: {found} != {policy.version}"
            )
    output = Path(output_dir).expanduser().absolute()
    _reject_output_symlinks(output)
    if output.exists():
        if not output.is_dir() or any(output.iterdir()):
            raise VcsAuditError("VCS audit output directory is not empty")
    output.mkdir(parents=True, exist_ok=True)
    _reject_output_symlinks(output)
    registry = output / "registry-requirements.txt"
    registry.write_text("".join("".join(block) for block in registry_blocks), encoding="utf-8")
    releases: list[Path] = []
    for policy in policy_items:
        release = output / f"{_normalize_name(policy.name)}-release-requirement.txt"
        release.write_text(f"{policy.name}=={policy.version}\n", encoding="utf-8")
        releases.append(release)
    plan_payload = {
        "schema": "skcapstone-vcs-audit-plan/v1",
        "registry_requirements": registry.name,
        "vcs": [
            {
                "name": item.name,
                "canonical_url": item.canonical_url,
                "commit": item.commit,
                "version": item.version,
                "release_requirement": releases[index].name,
            }
            for index, item in enumerate(policy_items)
        ],
    }
    plan_path = output / "plan.json"
    plan_path.write_text(
        json.dumps(plan_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    registry_sha = _sha256_bytes(registry.read_bytes())
    release_sha = tuple(_sha256_bytes(path.read_bytes()) for path in releases)
    plan_sha = _sha256_bytes(plan_path.read_bytes())
    for path in (registry, *releases, plan_path):
        path.chmod(0o444)
    return VcsAuditPlan(
        output,
        registry,
        tuple(releases),
        policy_items,
        registry_sha,
        release_sha,
        plan_sha,
    )


def _default_runner(command: list[str]) -> object:
    """Run one pip-audit command without a shell."""
    return subprocess.run(command, capture_output=True, text=True, check=False)


def _result_payload(command: list[str], result: object) -> dict:
    """Normalize injected or subprocess results."""
    if isinstance(result, dict):
        return {
            "command": command,
            "returncode": int(result.get("returncode", 1)),
            "stdout": _sanitize_output(result.get("stdout", "")),
            "stderr": _sanitize_output(result.get("stderr", "")),
        }
    return {
        "command": command,
        "returncode": int(getattr(result, "returncode", 1)),
        "stdout": _sanitize_output(getattr(result, "stdout", "")),
        "stderr": _sanitize_output(getattr(result, "stderr", "")),
    }


def run_vcs_audit(
    plan: VcsAuditPlan,
    *,
    runner: Callable[[list[str]], object] = _default_runner,
    executable: str = "pip-audit",
) -> VcsAuditReceipt:
    """Run and reconcile the hash-required and release-query audit halves."""
    _reject_output_symlinks(plan.output_dir)
    registry_bytes = _read_bound(plan.registry_requirements, plan.registry_sha256)
    if len(plan.vcs_release_requirements) != len(plan.release_sha256):
        raise VcsAuditError("VCS audit release plan is incomplete")
    release_bytes = tuple(
        _read_bound(path, expected)
        for path, expected in zip(plan.vcs_release_requirements, plan.release_sha256, strict=True)
    )
    _read_bound(plan.output_dir / "plan.json", plan.plan_sha256)
    commands: list[list[str]] = []
    with tempfile.TemporaryDirectory(prefix="skcapstone-vcs-audit-") as raw:
        execution = Path(raw)
        if registry_bytes.strip():
            registry = execution / "registry-requirements.txt"
            registry.write_bytes(registry_bytes)
            commands.append(
                [
                    executable,
                    "--strict",
                    "--require-hashes",
                    "--disable-pip",
                    "--requirement",
                    str(registry),
                ]
            )
        for index, value in enumerate(release_bytes):
            release = execution / f"vcs-release-{index}.txt"
            release.write_bytes(value)
            commands.append(
                [
                    executable,
                    "--strict",
                    "--no-deps",
                    "--disable-pip",
                    "--requirement",
                    str(release),
                ]
            )
        results = []
        for command in commands:
            try:
                result = runner(command)
            except OSError:
                raise VcsAuditError("vulnerability audit executable is unavailable") from None
            results.append(_result_payload(command, result))
    passed = all(item["returncode"] == 0 for item in results)
    receipt = {
        "schema": "skcapstone-vcs-audit-receipt/v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "passed": passed,
        "registry_hash_enforcement": True,
        "registry_sha256": plan.registry_sha256,
        "release_sha256": list(plan.release_sha256),
        "plan_sha256": plan.plan_sha256,
        "results": results,
        "limitations": (
            "Release vulnerability services index VCS packages by canonical name and version, "
            "so the separate query does not independently attest the audited commit bytes. "
            "The lock, exported requirement, installed version, URL, and immutable commit checks "
            "provide that identity boundary."
        ),
    }
    receipt_path = plan.output_dir / "audit-receipt.json"
    _reject_output_symlinks(plan.output_dir)
    _write_receipt(receipt_path, json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return VcsAuditReceipt(passed, receipt_path)
