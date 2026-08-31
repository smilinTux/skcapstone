from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from skcapstone.fleet.runtime_artifacts import (
    DeploymentError,
    ManifestError,
    guard_service_runtime,
    inspect_python_environment,
    parse_manifest,
    promote,
    validate_canonical_repository,
)


def digest(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def manifest_payload(artifact: Path, data: bytes, **overrides):
    artifact_digest = digest(data)
    payload = {
        "schema": "skfleet-service-runtime/v1",
        "service": "skcapstone-daemon",
        "repository": "https://github.com/smilinTux/skcapstone.git",
        "commit": "1" * 40,
        "tree": "2" * 40,
        "runtime_kind": "python-wheel",
        "artifacts": [
            {"name": "skcapstone.whl", "path": str(artifact), "digest": artifact_digest}
        ],
        "dependency_lock": "sha256:" + "3" * 64,
        "configuration_digest": "sha256:" + "4" * 64,
        "unit_digest": "sha256:" + "5" * 64,
        "host": "chiap03",
        "health_probe": {"kind": "exec", "target": "probe:skcapstone", "timeout_s": 30},
        "rollback_artifact": artifact_digest,
        "data_refs": ["data:coordination"],
        "credential_refs": ["credential:skgit-token"],
    }
    payload.update(overrides)
    return payload


def test_manifest_pins_complete_contract_and_derives_stable_version(tmp_path):
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"wheel")

    first = parse_manifest(manifest_payload(wheel, b"wheel"))
    second = parse_manifest(manifest_payload(wheel, b"wheel"))

    assert first.version == second.version
    assert first.repository == "https://github.com/smilinTux/skcapstone.git"
    assert first.credential_refs == ("credential:skgit-token",)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "main"),
        ("tree", "HEAD"),
        ("dependency_lock", "requirements.txt"),
        ("configuration_digest", "latest"),
        ("unit_digest", "sha256:ABC"),
        ("runtime_kind", "checkout"),
    ],
)
def test_manifest_rejects_unpinned_fields(tmp_path, field, value):
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"wheel")
    with pytest.raises(ManifestError):
        parse_manifest(manifest_payload(wheel, b"wheel", **{field: value}))


def test_manifest_is_closed_and_references_have_no_values(tmp_path):
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"wheel")
    payload = manifest_payload(wheel, b"wheel")
    payload["credential_values"] = {"credential:skgit-token": "secret"}
    with pytest.raises(ManifestError, match="fields are not closed"):
        parse_manifest(payload)


@pytest.mark.parametrize(
    ("repository", "allowed"),
    [
        ("https://skgit.skstack01.douno.it/smilinTux/SKLegal.git", True),
        ("https://skgit.skstack01.douno.it/smilinTux/HammerTime.git", True),
        ("https://github.com/smilinTux/SKLegal.git", False),
        ("https://github.com/smilinTux/HammerTime.git", False),
        ("https://github.com/smilinTux/skcapstone.git", True),
        ("https://skgit.skstack01.douno.it/smilinTux/skcapstone.git", False),
    ],
)
def test_canonical_remote_policy(repository, allowed):
    if allowed:
        assert validate_canonical_repository(repository) == repository
    else:
        with pytest.raises(ManifestError):
            validate_canonical_repository(repository)


def test_python_runtime_accepts_only_wheels(tmp_path):
    source = tmp_path / "source.tar.gz"
    source.write_bytes(b"source")
    with pytest.raises(ManifestError, match="wheels only"):
        parse_manifest(manifest_payload(source, b"source"))


def test_identity_mismatch_happens_before_mutation(tmp_path):
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"tampered")
    runtime_root = tmp_path / "runtime"
    parsed = parse_manifest(manifest_payload(wheel, b"expected"))

    with pytest.raises(DeploymentError, match="identity mismatch"):
        promote(parsed, runtime_root, start=lambda _: True, qualify=lambda _: True)

    assert not runtime_root.exists()


def test_atomic_promotion_records_installed_version(tmp_path):
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"wheel")
    parsed = parse_manifest(manifest_payload(wheel, b"wheel"))
    runtime_root = tmp_path / "runtime"

    destination = promote(parsed, runtime_root, start=lambda _: True, qualify=lambda _: True)

    current = runtime_root / parsed.service / "current"
    receipt = json.loads((runtime_root / parsed.service / "installed.json").read_text())
    assert current.is_symlink()
    assert Path(os.readlink(current)) == destination
    assert receipt["version"] == parsed.version
    assert receipt["artifacts"] == {"skcapstone.whl": digest(b"wheel")}


@pytest.mark.parametrize("start_ok,health_ok", [(False, True), (True, False)])
def test_failed_start_or_health_rolls_back_automatically(tmp_path, start_ok, health_ok):
    runtime_root = tmp_path / "runtime"
    service_root = runtime_root / "skcapstone-daemon"
    old = service_root / "versions" / "old-version"
    old.mkdir(parents=True)
    current = service_root / "current"
    current.symlink_to(old)
    wheel = tmp_path / "skcapstone.whl"
    wheel.write_bytes(b"wheel")
    parsed = parse_manifest(manifest_payload(wheel, b"wheel"))

    with pytest.raises(DeploymentError, match="startup or health"):
        promote(parsed, runtime_root, start=lambda _: start_ok, qualify=lambda _: health_ok)

    assert Path(os.readlink(current)) == old
    receipt = json.loads((service_root / "installed.json").read_text())
    assert receipt == {
        "rolled_back": True,
        "schema": "skfleet-service-runtime/v1",
        "service": "skcapstone-daemon",
        "version": str(old),
    }


def test_guard_blocks_checkout_tmp_shared_venv_and_allows_isolated_dev(tmp_path):
    dev = tmp_path / "developer" / "card-venv" / "bin" / "python"
    violations = guard_service_runtime(
        exec_paths=[
            Path("/tmp/copied-service/start.sh"),
            Path.home() / ".skcapstone/fleet/workspaces/card/service.py",
            Path.home() / ".skenv/bin/python",
            dev,
        ],
        developer_roots=[tmp_path / "developer"],
    )

    assert len(violations) == 3
    assert any("temporary path" in item for item in violations)
    assert any("shared or development virtual environment" in item for item in violations)
    assert all(str(dev) not in item for item in violations)


def test_direct_python_environment_inspection_detects_editable_install(tmp_path):
    python = tmp_path / "python"
    metadata = '[{"name":"bad","direct":"{\\"dir_info\\":{\\"editable\\":true}}"}]'
    python.write_text(f"#!/bin/sh\nprintf '%s\\n' '{metadata}'\n")
    python.chmod(0o755)

    assert inspect_python_environment(python) == ["editable install: bad"]
