"""Content-bound local continuation without discarding a prior workspace."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet import builder_checkpoint as checkpoint


def git(workspace: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(workspace), *args], text=True, stderr=subprocess.PIPE
    ).strip()


@pytest.fixture
def source(tmp_path):
    workspace = tmp_path / "original"
    workspace.mkdir()
    git(workspace, "init", "--quiet")
    git(workspace, "config", "user.name", "Test Worker")
    git(workspace, "config", "user.email", "worker@example.invalid")
    git(workspace, "remote", "add", "origin", "https://example.invalid/source.git")
    (workspace / "code.py").write_text("initial = True\n")
    (workspace / ".gitignore").write_text("cache/\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "--quiet", "-m", "base")
    base = git(workspace, "rev-parse", "HEAD")
    (workspace / "code.py").write_text("initial = False\n")
    git(workspace, "commit", "--quiet", "-am", "preserved progress")
    request = {
        "card_id": "1234abcd",
        "request_id": "builder-request-1",
        "repository": "https://example.invalid/source.git",
        "base_ref": "main",
        "base_revision": base,
    }
    status = {
        "request_id": request["request_id"],
        "card_id": request["card_id"],
        "owner": "pi-builder-test-1234abcd",
        "claim_revision": "claim-1",
        "execution_host": "test-host",
        "execution_boot_id": "boot-1",
        "execution_unit": "builder-test.service",
        "workspace": str(workspace),
    }
    return workspace, request, status, tmp_path / "evidence" / "checkpoint.json"


def test_clean_committed_progress_restores_in_new_workspace(source, tmp_path):
    workspace, request, status, destination = source
    before = git(workspace, "rev-parse", "HEAD")
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    target = tmp_path / "resumed"
    assert checkpoint.restore_clean_checkpoint(receipt, request, target) == target
    assert git(target, "rev-parse", "HEAD") == before == receipt["head"]
    assert git(target, "rev-parse", "HEAD^{tree}") == receipt["tree"]
    assert git(target, "remote", "get-url", "origin") == request["repository"]
    assert git(target, "status", "--porcelain") == ""
    assert git(workspace, "rev-parse", "HEAD") == before
    assert not (target / ".git" / "objects" / "info" / "alternates").exists()


def test_exact_seal_replay_preserves_receipt(source):
    workspace, request, status, destination = source
    first = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    before = destination.read_bytes()
    assert checkpoint.seal_clean_checkpoint(workspace, request, status, destination) == first
    assert destination.read_bytes() == before


@pytest.mark.parametrize("untracked", [False, True])
def test_unsealed_work_is_preserved_and_refused(source, untracked):
    workspace, request, status, destination = source
    path = workspace / ("new.py" if untracked else "code.py")
    path.write_text("unfinished = True\n")
    with pytest.raises(ValueError, match="clean"):
        checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    assert path.read_text() == "unfinished = True\n"
    assert not destination.exists()


def test_ignored_material_stays_in_original_only(source, tmp_path):
    workspace, request, status, destination = source
    (workspace / "cache").mkdir()
    (workspace / "cache" / "scratch.txt").write_text("retained locally")
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    target = tmp_path / "resumed"
    checkpoint.restore_clean_checkpoint(receipt, request, target)
    assert (workspace / "cache" / "scratch.txt").read_text() == "retained locally"
    assert not (target / "cache").exists()


def test_changed_origin_is_refused(source):
    workspace, request, status, destination = source
    git(workspace, "remote", "set-url", "origin", "https://example.invalid/other.git")
    with pytest.raises(ValueError, match="repository"):
        checkpoint.seal_clean_checkpoint(workspace, request, status, destination)


def test_unrelated_candidate_is_refused(source):
    workspace, request, status, destination = source
    git(workspace, "checkout", "--quiet", "--orphan", "unrelated")
    git(workspace, "commit", "--quiet", "-m", "other history")
    with pytest.raises(ValueError, match="ancestry"):
        checkpoint.seal_clean_checkpoint(workspace, request, status, destination)


@pytest.mark.parametrize("field", ["request_id", "card_id"])
def test_mismatched_execution_binding_is_refused(source, field):
    workspace, request, status, destination = source
    status[field] = "other"
    with pytest.raises(ValueError, match="identity"):
        checkpoint.seal_clean_checkpoint(workspace, request, status, destination)


def test_existing_manifest_cannot_be_replaced_by_new_generation(source):
    workspace, request, status, destination = source
    checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    before = destination.read_bytes()
    status["claim_revision"] = "claim-2"
    with pytest.raises(ValueError, match="existing checkpoint"):
        checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    assert destination.read_bytes() == before


def test_tampered_manifest_is_refused(source, tmp_path):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    manifest = json.loads(destination.read_text())
    manifest["execution"]["claim_revision"] = "new"
    destination.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="hash"):
        checkpoint.restore_clean_checkpoint(receipt, request, tmp_path / "resumed")


def test_receipt_generation_must_match_hashed_manifest(source, tmp_path):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    receipt["claim_revision"] = "claim-2"
    with pytest.raises(ValueError, match="receipt"):
        checkpoint.restore_clean_checkpoint(receipt, request, tmp_path / "resumed")


@pytest.mark.parametrize("change", ["commit", "dirty", "request"])
def test_changed_input_after_seal_is_refused(source, tmp_path, change):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    if change in {"commit", "dirty"}:
        (workspace / "code.py").write_text("later = True\n")
        if change == "commit":
            git(workspace, "commit", "--quiet", "-am", "later")
    else:
        request["base_ref"] = "other"
    with pytest.raises(ValueError):
        checkpoint.restore_clean_checkpoint(receipt, request, tmp_path / "resumed")
    assert not (tmp_path / "resumed").exists()


def test_existing_target_is_never_deleted(source, tmp_path):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    target = tmp_path / "resumed"
    target.mkdir()
    (target / "keep").write_text("keep")
    with pytest.raises(ValueError, match="destination"):
        checkpoint.restore_clean_checkpoint(receipt, request, target)
    assert (target / "keep").read_text() == "keep"


def test_restore_inside_original_is_refused(source):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    with pytest.raises(ValueError, match="destination"):
        checkpoint.restore_clean_checkpoint(receipt, request, workspace / "nested")


def test_concurrently_created_empty_target_is_not_replaced(source, tmp_path, monkeypatch):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    target = tmp_path / "resumed"
    original = checkpoint._publish_workspace
    inode = []

    def concurrent_destination(temporary, published):
        published.mkdir()
        inode.append(published.stat().st_ino)
        original(temporary, published)

    monkeypatch.setattr(checkpoint, "_publish_workspace", concurrent_destination)
    with pytest.raises(ValueError, match="destination"):
        checkpoint.restore_clean_checkpoint(receipt, request, target)
    assert target.stat().st_ino == inode[0]
    assert list(target.iterdir()) == []
    assert not list(tmp_path.glob(".resumed.*"))


def test_failed_reconstruction_leaves_original_and_no_partial_target(
    source, tmp_path, monkeypatch
):
    workspace, request, status, destination = source
    receipt = checkpoint.seal_clean_checkpoint(workspace, request, status, destination)
    original = checkpoint._git

    def fail_fetch(root, *args):
        if args[0] == "fetch":
            raise ValueError("injected fetch failure")
        return original(root, *args)

    monkeypatch.setattr(checkpoint, "_git", fail_fetch)
    with pytest.raises(ValueError, match="fetch"):
        checkpoint.restore_clean_checkpoint(receipt, request, tmp_path / "resumed")
    assert workspace.is_dir()
    assert not (tmp_path / "resumed").exists()
    assert not list(tmp_path.glob(".resumed.*"))
