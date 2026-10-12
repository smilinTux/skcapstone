"""Operator artifacts retain exact source bindings and immutable publication."""

import subprocess

import pytest

from skcapstone.fleet import production_node_artifact as artifact
from skcapstone.fleet import production_test_node as node
from skcapstone.fleet import production_test_plan as plan


@pytest.fixture
def source(tmp_path, monkeypatch):
    root = tmp_path / "source"
    (root / "tests").mkdir(parents=True)
    for name in artifact.native.SOURCE_FILES:
        (root / name).write_text("" if name.endswith("mjs") else "{}")
    monkeypatch.setattr(node, "ARTIFACT_ROOT", tmp_path / "artifacts")
    runtime = tmp_path / "node"
    runtime.write_bytes(b"runtime")
    monkeypatch.setattr(node, "NODE", runtime)
    return root


def prepared(source, tmp_path):
    """Build a harmless dependency fixture that exercises the real digest."""
    root = tmp_path / "prepared"
    (root / "node_modules/example").mkdir(parents=True)
    for name in artifact.native.SOURCE_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((source / name).read_bytes())
    (root / "node_modules/example/index.js").write_text("module.exports = 42;")
    return root


def test_exact_immutable_install_is_idempotent_and_resolves(source, tmp_path):
    root = prepared(source, tmp_path)
    value = artifact.install(source, root)
    installed = node.ARTIFACT_ROOT / value["artifact_sha256"]
    assert (installed / "qualification-marker").read_bytes() == b"after!"
    assert not installed.stat().st_mode & 0o222
    assert node.artifact_digest(installed) == installed.name
    assert node.qualified_environment(source) == value
    assert artifact.install(source, root) == value
    assert list(node.ARTIFACT_ROOT.iterdir()) == [installed]


@pytest.mark.parametrize("kind", ["lock", "bootstrap", "escape", "modules-link", "manifest-link"])
def test_wrong_or_redirected_inputs_never_publish(source, tmp_path, kind):
    root = prepared(source, tmp_path)
    if kind in {"lock", "bootstrap"}:
        name = "package-lock.json" if kind == "lock" else "tests/_setup.mjs"
        (root / name).write_text("changed")
    elif kind == "escape":
        (root / "node_modules/escape").symlink_to(tmp_path)
    elif kind == "modules-link":
        (root / "node_modules").rename(tmp_path / "outside")
        (root / "node_modules").symlink_to(tmp_path / "outside")
    else:
        (root / "package.json").unlink()
        (root / "package.json").symlink_to(source / "package.json")
    with pytest.raises(plan.TestEvidenceError):
        artifact.install(source, root)
    assert not node.ARTIFACT_ROOT.exists() or all(
        p.name.startswith(".node-artifact-") for p in node.ARTIFACT_ROOT.iterdir()
    )


def test_prepare_cleans_environment_and_never_runs_lifecycle_scripts(
    source, tmp_path, monkeypatch
):
    run = tmp_path / "run"
    calls = []

    def npm(argv, **kwargs):
        calls.append((argv, kwargs))
        (kwargs["cwd"] / "node_modules").mkdir()

    monkeypatch.setenv("NODE_OPTIONS", "--import /malicious.js")
    monkeypatch.setenv("NPM_CONFIG_IGNORE_SCRIPTS", "false")
    monkeypatch.setattr(artifact.subprocess, "run", npm)
    result = artifact.prepare(source, run, offline=True)
    argv, kwargs = calls[0]
    assert argv == [
        "/usr/bin/npm",
        "ci",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        "--offline",
    ]
    assert kwargs["cwd"] == result
    assert "NODE_OPTIONS" not in kwargs["env"]
    assert "NPM_CONFIG_IGNORE_SCRIPTS" not in kwargs["env"]
    assert kwargs["env"]["PATH"].startswith(str(node.NODE.parent) + ":")
    assert artifact.source_hashes(result) == artifact.source_hashes(source)
    with pytest.raises(FileExistsError):
        artifact.prepare(source, run)


def test_failed_npm_never_publishes(source, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])

    monkeypatch.setattr(artifact.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        artifact.prepare(source, tmp_path / "run")
    assert not node.ARTIFACT_ROOT.exists()


def test_operator_receipt_records_installation_without_forging_test_pass(
    source, tmp_path, monkeypatch
):
    import json
    import sys

    root = prepared(source, tmp_path)
    receipt = tmp_path / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "artifact",
            "install",
            str(source),
            str(root),
            "--operator",
            "jarvis",
            "--receipt",
            str(receipt),
        ],
    )
    artifact.main()
    result = json.loads(receipt.read_text())
    assert result["operator"] == "jarvis" and result["product_tests_run"] is False
    assert node.qualified_environment(source) == result["environment"]
    original = receipt.read_bytes()
    with pytest.raises(plan.TestEvidenceError, match="already exists"):
        artifact.main()
    assert receipt.read_bytes() == original
