"""Tests for closed fleet runtime manifests and atomic activation."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "fleet" / "skfleet_runtime_bundle.py"
SPEC = importlib.util.spec_from_file_location("skfleet_runtime_bundle", MODULE_PATH)
assert SPEC and SPEC.loader
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_payload(tmp_path: Path, *, include_wrapper: bool = True, evidence: bool = True):
    payload = tmp_path / "payload"
    launcher = payload / "scripts/fleet/skfleet-rotate.py"
    wrapper = payload / "scripts/fleet/skfleet-worker-wrapper.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        'import os\nwrapper=os.path.join(os.path.dirname(__file__),"skfleet-worker-wrapper.py")\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    if include_wrapper:
        wrapper.write_text("#!/usr/bin/env python3\nprint('worker-exit')\n", encoding="utf-8")
        wrapper.chmod(0o755)
    owner = __import__("pwd").getpwuid(os.getuid()).pw_name
    files = []
    for path, required_by in (
        (launcher, []),
        (wrapper, ["scripts/fleet/skfleet-rotate.py"]),
    ):
        if path.exists():
            files.append({
                "path": path.relative_to(payload).as_posix(),
                "sha256": _digest(path),
                "size": path.stat().st_size,
                "owner": owner,
                "mode": "0755",
                "source_commit": "a" * 40,
                "required_by": required_by,
            })
    manifest = {
        "schema_version": 1,
        "release": "0.15.134-test",
        "launcher": "scripts/fleet/skfleet-rotate.py",
        "files": files,
    }
    if evidence:
        manifest["activation_evidence"] = {
            name: {"artifact": f"evidence/{name}.json", "sha256": "b" * 64}
            for name in ("independent_review", "release", "canary", "five_host_rollout")
        }
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path, payload, manifest


def test_closed_manifest_verifies_clean_payload(tmp_path: Path) -> None:
    manifest, payload, _ = _make_payload(tmp_path)
    loaded = bundle.verify_payload(manifest, payload)
    bundle.verify_static_closure(manifest, payload, loaded["launcher"])


@pytest.mark.parametrize("mutation", ["missing", "digest", "mode", "extra"])
def test_payload_rejects_partial_or_mixed_release(tmp_path: Path, mutation: str) -> None:
    manifest, payload, _ = _make_payload(tmp_path)
    wrapper = payload / "scripts/fleet/skfleet-worker-wrapper.py"
    if mutation == "missing":
        wrapper.unlink()
    elif mutation == "digest":
        wrapper.write_text("mixed release", encoding="utf-8")
    elif mutation == "mode":
        wrapper.chmod(0o644)
    else:
        (payload / "unreviewed-helper.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(bundle.BundleError):
        bundle.verify_payload(manifest, payload)


def test_static_closure_rejects_measured_launcher_without_wrapper(tmp_path: Path) -> None:
    manifest, payload, data = _make_payload(tmp_path)
    data["files"] = [entry for entry in data["files"] if not entry["path"].endswith("wrapper.py")]
    manifest.write_text(json.dumps(data), encoding="utf-8")
    (payload / "scripts/fleet/skfleet-worker-wrapper.py").unlink()
    bundle.verify_payload(manifest, payload)
    with pytest.raises(bundle.BundleError, match="absent from manifest"):
        bundle.verify_static_closure(manifest, payload, data["launcher"])


def test_clean_runtime_launch_and_exit(tmp_path: Path) -> None:
    manifest, payload, _ = _make_payload(tmp_path)
    runtime = tmp_path / "runtime"
    destination = bundle.install(
        manifest,
        payload,
        runtime,
        ["python3", "scripts/fleet/skfleet-worker-wrapper.py"],
    )
    assert (runtime / "current").resolve() == destination.resolve()
    assert (destination / "runtime-manifest.json").is_file()


def test_health_failure_restores_complete_previous_manifest(tmp_path: Path) -> None:
    manifest, payload, data = _make_payload(tmp_path / "first")
    runtime = tmp_path / "runtime"
    first = bundle.install(manifest, payload, runtime)
    second_manifest, second_payload, second = _make_payload(tmp_path / "second")
    second["release"] = "0.15.135-test"
    second_manifest.write_text(json.dumps(second), encoding="utf-8")
    with pytest.raises(Exception):
        bundle.install(second_manifest, second_payload, runtime, ["python3", "-c", "raise SystemExit(9)"])
    assert (runtime / "current").resolve() == first.resolve()
    bundle.verify_payload(first / "runtime-manifest.json", first)


def test_activation_rejects_absent_review_or_rollout_evidence(tmp_path: Path) -> None:
    manifest, payload, data = _make_payload(tmp_path, evidence=False)
    with pytest.raises(bundle.BundleError, match="activation_evidence"):
        bundle.install(manifest, payload, tmp_path / "runtime")
