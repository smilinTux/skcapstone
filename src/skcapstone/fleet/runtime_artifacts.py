"""Immutable service runtime artifact contract and qualification guard.

This module is deliberately provider free. It validates deployment inputs and
implements a filesystem transaction, but never invokes systemd, downloads
source, or reads credential values.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA = "skfleet-service-runtime/v1"
RUNTIME_KINDS = frozenset({"python-wheel", "node-bundle", "script-bundle"})
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_ID = re.compile(r"^[0-9a-f]{40,64}$")
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
_MUTABLE_PARTS = frozenset({"tmp", "worktree", "worktrees", "checkout", "checkouts"})
_SHARED_VENV_NAMES = frozenset({".skenv", "venv", ".venv"})


class ManifestError(ValueError):
    """The manifest is incomplete, mutable, or violates repository policy."""


class DeploymentError(RuntimeError):
    """Artifact verification, promotion, startup, or qualification failed."""


@dataclass(frozen=True)
class Artifact:
    """One content addressed input to a service runtime."""

    name: str
    path: Path
    digest: str


@dataclass(frozen=True)
class RuntimeManifest:
    """Validated version 1 service runtime manifest."""

    service: str
    repository: str
    commit: str
    tree: str
    runtime_kind: str
    artifacts: tuple[Artifact, ...]
    dependency_lock: str
    configuration_digest: str
    unit_digest: str
    host: str
    health_probe: Mapping[str, Any]
    rollback_artifact: str
    data_refs: tuple[str, ...]
    credential_refs: tuple[str, ...]

    @property
    def version(self) -> str:
        """Stable installed version derived only from immutable inputs."""
        body = json.dumps(
            {
                "schema": SCHEMA,
                "service": self.service,
                "commit": self.commit,
                "tree": self.tree,
                "artifacts": [(item.name, item.digest) for item in self.artifacts],
                "configuration_digest": self.configuration_digest,
                "unit_digest": self.unit_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(body).hexdigest()


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ManifestError(f"{field} must be a lowercase sha256 digest")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _REF.fullmatch(value):
        raise ManifestError(f"{field} must be an opaque reference, not a value")
    return value


def _repository_name(repository: str) -> str:
    parsed = urlparse(repository)
    path = parsed.path if parsed.scheme else repository.split(":", 1)[-1]
    return Path(path.removesuffix(".git")).name.lower()


def validate_canonical_repository(repository: Any) -> str:
    """Enforce SKGit for SKLegal and HammerTime and GitHub for all others."""
    if not isinstance(repository, str) or not repository:
        raise ManifestError("repository must be a canonical URL")
    parsed = urlparse(repository)
    host = (parsed.hostname or "").lower()
    name = _repository_name(repository)
    skgit_only = name in {"sklegal", "hammertime"}
    if skgit_only and host != "skgit.skstack01.douno.it":
        raise ManifestError(f"{name} must use its SKGit canonical repository")
    if not skgit_only and host != "github.com":
        raise ManifestError(f"{name} must use its GitHub canonical repository")
    return repository


def parse_manifest(payload: Mapping[str, Any]) -> RuntimeManifest:
    """Parse a closed manifest and reject unknown or mutable representations."""
    required = {
        "schema",
        "service",
        "repository",
        "commit",
        "tree",
        "runtime_kind",
        "artifacts",
        "dependency_lock",
        "configuration_digest",
        "unit_digest",
        "host",
        "health_probe",
        "rollback_artifact",
        "data_refs",
        "credential_refs",
    }
    if not isinstance(payload, Mapping) or set(payload) != required:
        missing = sorted(required - set(payload)) if isinstance(payload, Mapping) else []
        extra = sorted(set(payload) - required) if isinstance(payload, Mapping) else []
        raise ManifestError(f"manifest fields are not closed; missing={missing}, extra={extra}")
    if payload["schema"] != SCHEMA:
        raise ManifestError(f"schema must be {SCHEMA}")
    service = _identifier(payload["service"], "service")
    host = _identifier(payload["host"], "host")
    commit = payload["commit"]
    tree = payload["tree"]
    if not isinstance(commit, str) or not _GIT_ID.fullmatch(commit):
        raise ManifestError("commit must be a full git object id")
    if not isinstance(tree, str) or not _GIT_ID.fullmatch(tree):
        raise ManifestError("tree must be a full git object id")
    runtime_kind = payload["runtime_kind"]
    if runtime_kind not in RUNTIME_KINDS:
        raise ManifestError(f"runtime_kind must be one of {sorted(RUNTIME_KINDS)}")

    raw_artifacts = payload["artifacts"]
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        raise ManifestError("artifacts must be a nonempty list")
    artifacts: list[Artifact] = []
    names: set[str] = set()
    for index, item in enumerate(raw_artifacts):
        if not isinstance(item, Mapping) or set(item) != {"name", "path", "digest"}:
            raise ManifestError(f"artifacts[{index}] must contain name, path, and digest")
        name = _identifier(item["name"], f"artifacts[{index}].name")
        if name in names:
            raise ManifestError(f"duplicate artifact name: {name}")
        names.add(name)
        path = Path(str(item["path"]))
        if not path.is_absolute():
            raise ManifestError(f"artifacts[{index}].path must be absolute")
        artifacts.append(
            Artifact(name, path, _digest(item["digest"], f"artifacts[{index}].digest"))
        )

    health_probe = payload["health_probe"]
    if not isinstance(health_probe, Mapping) or set(health_probe) != {
        "kind",
        "target",
        "timeout_s",
    }:
        raise ManifestError("health_probe must contain only kind, target, and timeout_s")
    if health_probe["kind"] not in {"exec", "http", "tcp"}:
        raise ManifestError("health_probe.kind is unsupported")
    _identifier(health_probe["target"], "health_probe.target")
    timeout = health_probe["timeout_s"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
        raise ManifestError("health_probe.timeout_s must be an integer from 1 through 300")

    refs: dict[str, tuple[str, ...]] = {}
    for field in ("data_refs", "credential_refs"):
        values = payload[field]
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise ManifestError(f"{field} must be a unique list")
        refs[field] = tuple(_identifier(value, f"{field}[]") for value in values)

    rollback = _digest(payload["rollback_artifact"], "rollback_artifact")
    artifact_digests = {item.digest for item in artifacts}
    if rollback not in artifact_digests:
        raise ManifestError("rollback_artifact must identify an artifact present in the manifest")
    if runtime_kind == "python-wheel" and any(item.path.suffix != ".whl" for item in artifacts):
        raise ManifestError("python runtimes may contain wheels only")

    return RuntimeManifest(
        service=service,
        repository=validate_canonical_repository(payload["repository"]),
        commit=commit,
        tree=tree,
        runtime_kind=runtime_kind,
        artifacts=tuple(artifacts),
        dependency_lock=_digest(payload["dependency_lock"], "dependency_lock"),
        configuration_digest=_digest(payload["configuration_digest"], "configuration_digest"),
        unit_digest=_digest(payload["unit_digest"], "unit_digest"),
        host=host,
        health_probe=dict(health_probe),
        rollback_artifact=rollback,
        data_refs=refs["data_refs"],
        credential_refs=refs["credential_refs"],
    )


def load_manifest(path: Path) -> RuntimeManifest:
    """Load JSON without permitting duplicate keys."""

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ManifestError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    return parse_manifest(payload)


def sha256_file(path: Path) -> str:
    """Hash a regular, non-symlink artifact."""
    if path.is_symlink() or not path.is_file():
        raise DeploymentError(f"artifact is not a regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def verify_artifacts(manifest: RuntimeManifest) -> None:
    """Verify every byte before any deployment mutation."""
    for artifact in manifest.artifacts:
        actual = sha256_file(artifact.path)
        if actual != artifact.digest:
            raise DeploymentError(
                f"artifact identity mismatch for {artifact.name}: "
                f"expected {artifact.digest}, got {actual}"
            )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def promote(
    manifest: RuntimeManifest,
    root: Path,
    *,
    start: Callable[[Path], bool],
    qualify: Callable[[Mapping[str, Any]], bool],
) -> Path:
    """Verify, stage, atomically promote, record, and roll back on failure.

    ``start`` and ``qualify`` are injected boundaries. A caller may bind them
    to a service manager and health probe only after its own authorization.
    """
    verify_artifacts(manifest)
    service_root = root / manifest.service
    versions = service_root / "versions"
    destination = versions / manifest.version
    staging = versions / f".{manifest.version}.{os.getpid()}.staging"
    current = service_root / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    if current.exists() and not current.is_symlink():
        raise DeploymentError("current runtime pointer must be a symlink")

    try:
        staging.mkdir(parents=True, exist_ok=False)
        for artifact in manifest.artifacts:
            shutil.copyfile(artifact.path, staging / artifact.name)
        for copied, artifact in zip(
            sorted(staging.iterdir()), sorted(manifest.artifacts, key=lambda a: a.name)
        ):
            if copied.name != artifact.name or sha256_file(copied) != artifact.digest:
                raise DeploymentError("staged artifact identity mismatch")
        os.replace(staging, destination)
        service_root.mkdir(parents=True, exist_ok=True)
        new_link = service_root / f".current.{os.getpid()}.tmp"
        os.symlink(str(destination), new_link)
        os.replace(new_link, current)
        receipt = {
            "schema": SCHEMA,
            "service": manifest.service,
            "version": manifest.version,
            "commit": manifest.commit,
            "tree": manifest.tree,
            "artifacts": {item.name: item.digest for item in manifest.artifacts},
            "previous": previous,
        }
        _atomic_json(service_root / "installed.json", receipt)
        if not start(destination) or not qualify(manifest.health_probe):
            raise DeploymentError("startup or health qualification failed")
        return destination
    except Exception as exc:
        if staging.exists():
            shutil.rmtree(staging)
        if (
            destination.exists()
            and current.is_symlink()
            and os.readlink(current) == str(destination)
        ):
            rollback_link = service_root / f".current.{os.getpid()}.rollback"
            if previous is None:
                current.unlink(missing_ok=True)
            else:
                os.symlink(previous, rollback_link)
                os.replace(rollback_link, current)
            _atomic_json(
                service_root / "installed.json",
                {
                    "schema": SCHEMA,
                    "service": manifest.service,
                    "version": previous,
                    "rolled_back": True,
                },
            )
        if isinstance(exc, DeploymentError):
            raise
        raise DeploymentError(str(exc)) from exc


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def mutable_runtime_reason(path: Path, *, developer_roots: Sequence[Path] = ()) -> str | None:
    """Return a violation for a service path while allowing explicit dev roots."""
    expanded = path.expanduser()
    resolved = expanded.resolve(strict=False)
    if any(
        _is_within(resolved, root.expanduser().resolve(strict=False)) for root in developer_roots
    ):
        return None
    parts = {part.lower() for part in (*expanded.parts, *resolved.parts)}
    lexical = expanded.as_posix().lower()
    if parts & _MUTABLE_PARTS or "fleet/workspaces" in lexical:
        return "runtime resolves through a checkout, worktree, or temporary path"
    if parts & _SHARED_VENV_NAMES:
        return "runtime resolves through a shared or development virtual environment"
    return None


def inspect_python_environment(python: Path) -> list[str]:
    """Detect editable metadata even when pip was called by absolute path."""
    script = (
        "import importlib.metadata as m,json;"
        "print(json.dumps([{'name':d.metadata.get('Name',''),'direct':"
        "d.read_text('direct_url.json')} for d in m.distributions()]))"
    )
    result = subprocess.run([str(python), "-I", "-c", script], capture_output=True, text=True)
    if result.returncode != 0:
        return [f"cannot inspect interpreter {python}"]
    try:
        distributions = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [f"interpreter returned invalid metadata: {python}"]
    violations = []
    for distribution in distributions:
        direct = distribution.get("direct")
        if not direct:
            continue
        try:
            metadata = json.loads(direct)
        except json.JSONDecodeError:
            violations.append(f"invalid direct_url.json for {distribution.get('name')}")
            continue
        if metadata.get("dir_info", {}).get("editable") is True:
            violations.append(f"editable install: {distribution.get('name')}")
    return violations


def guard_service_runtime(
    *,
    exec_paths: Sequence[Path],
    python: Path | None = None,
    developer_roots: Sequence[Path] = (),
) -> list[str]:
    """Mechanically qualify service executable and import paths."""
    violations: list[str] = []
    for path in exec_paths:
        reason = mutable_runtime_reason(path, developer_roots=developer_roots)
        if reason:
            violations.append(f"{path}: {reason}")
    if python is not None:
        reason = mutable_runtime_reason(python, developer_roots=developer_roots)
        if reason:
            violations.append(f"{python}: {reason}")
        violations.extend(inspect_python_environment(python))
    return violations
