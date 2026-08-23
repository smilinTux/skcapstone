"""Immutable review checkpoint and evidence sealing tests."""

from __future__ import annotations

import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest

import skcapstone.qualification.checkpoint as checkpoint_module
from skcapstone.qualification.checkpoint import (
    CheckpointError,
    create_checkpoint,
    record_review,
    seal_completion,
    verify_checkpoint,
)


def _workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "docs" / "evidence.md").write_text("draft\n", encoding="utf-8")
    return root


def test_checkpoint_inventory_is_sorted_deterministic_and_snapshotted(tmp_path) -> None:
    root = _workspace(tmp_path)
    first = create_checkpoint(
        root,
        ["src/app.py", "docs/evidence.md"],
        tmp_path / "checkpoint-a",
        created_at="2026-08-20T00:00:00+00:00",
    )
    second = create_checkpoint(
        root,
        ["docs/evidence.md", "src/app.py"],
        tmp_path / "checkpoint-b",
        created_at="2026-08-20T00:00:00+00:00",
    )

    assert first.inventory_sha256 == second.inventory_sha256
    assert first.inventory_path.read_text() == second.inventory_path.read_text()
    assert (first.directory / "source" / "src" / "app.py").read_text() == "print('ok')\n"
    assert not first.inventory_path.stat().st_mode & stat.S_IWUSR
    assert not (first.directory / "source").stat().st_mode & stat.S_IWUSR


def test_checkpoint_rejects_duplicate_escape_and_symlink_paths(tmp_path) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)

    with pytest.raises(CheckpointError, match="duplicate"):
        create_checkpoint(root, ["src/app.py", "src/app.py"], tmp_path / "dup")
    with pytest.raises(CheckpointError, match="canonical"):
        create_checkpoint(root, ["../outside.txt"], tmp_path / "escape")
    with pytest.raises(CheckpointError, match="canonical"):
        create_checkpoint(root, ["src/../src/app.py"], tmp_path / "normalized")
    with pytest.raises(CheckpointError, match="symlink"):
        create_checkpoint(root, ["link.txt"], tmp_path / "link")


def test_source_drift_marks_verification_unsealed(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    (root / "src" / "app.py").write_text("print('changed')\n", encoding="utf-8")

    receipt = verify_checkpoint(checkpoint.directory, root)

    assert receipt.sealed is False
    assert receipt.changed_paths == ("src/app.py",)


def test_multiple_reviewers_bind_exact_inventory(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")

    first = record_review(checkpoint.directory, root, reviewer="lovelace", disposition="accept")
    second = record_review(checkpoint.directory, root, reviewer="einstein", disposition="accept")

    assert first.inventory_sha256 == checkpoint.inventory_sha256
    assert second.inventory_sha256 == checkpoint.inventory_sha256
    assert len(list((checkpoint.directory / "reviews").glob("*/receipt.json"))) == 2


def test_review_refuses_drifted_source(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    (root / "src" / "app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CheckpointError, match="source drift"):
        record_review(checkpoint.directory, root, reviewer="reviewer", disposition="accept")


def test_evidence_only_completion_diff_is_explicit(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(
        root,
        ["src/app.py"],
        tmp_path / "checkpoint",
    )
    record_review(checkpoint.directory, root, reviewer="lovelace", disposition="accept")
    record_review(checkpoint.directory, root, reviewer="einstein", disposition="accept")
    (root / "docs" / "evidence.md").write_text("sealed receipt\n", encoding="utf-8")

    completion = seal_completion(
        checkpoint.directory,
        root,
        evidence_allowlist={"docs/evidence.md"},
    )

    assert completion.accepted_inventory_sha256 == checkpoint.inventory_sha256
    assert completion.changed_paths == ("docs/evidence.md",)
    payload = json.loads(completion.receipt_path.read_text(encoding="utf-8"))
    assert payload["source_changes"] == []
    assert payload["evidence_changes"] == ["docs/evidence.md"]
    assert not completion.inventory_path.stat().st_mode & stat.S_IWUSR
    assert not completion.receipt_path.stat().st_mode & stat.S_IWUSR
    assert not completion.receipt_path.parent.stat().st_mode & stat.S_IWUSR

    with pytest.raises(CheckpointError, match="already been sealed"):
        seal_completion(
            checkpoint.directory,
            root,
            evidence_allowlist={"docs/evidence.md"},
        )


def test_completion_rejects_source_change_and_rejected_review(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(
        root,
        ["src/app.py"],
        tmp_path / "checkpoint",
    )
    record_review(checkpoint.directory, root, reviewer="reviewer", disposition="reject")
    (root / "src" / "app.py").write_text("changed\n", encoding="utf-8")

    with pytest.raises(CheckpointError, match="accepted review"):
        seal_completion(
            checkpoint.directory,
            root,
            evidence_allowlist={"docs/evidence.md"},
        )


def test_completion_inventory_cannot_hash_itself(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    record_review(checkpoint.directory, root, reviewer="reviewer", disposition="accept")

    with pytest.raises(CheckpointError, match="self-referential"):
        seal_completion(
            checkpoint.directory,
            root,
            evidence_allowlist={"completion-inventory.sha256"},
        )


def test_completion_rejects_forged_or_incomplete_review_receipt(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    reviews = checkpoint.directory / "reviews"
    reviews.mkdir()
    forged = reviews / "forged"
    forged.mkdir()
    (forged / "receipt.json").write_text(
        json.dumps(
            {
                "reviewer": "forged",
                "disposition": "accept",
                "inventory_sha256": checkpoint.inventory_sha256,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointError, match="incomplete|unpaired"):
        seal_completion(checkpoint.directory, root, evidence_allowlist=set())


def test_completion_rejects_accepted_source_in_evidence_allowlist(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    record_review(checkpoint.directory, root, reviewer="reviewer", disposition="accept")

    with pytest.raises(CheckpointError, match="cannot be reclassified"):
        seal_completion(
            checkpoint.directory,
            root,
            evidence_allowlist={"src/app.py"},
        )


def test_review_replay_is_idempotent_and_pair_is_atomic(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    kwargs = {
        "reviewer": "reviewer",
        "disposition": "accept",
        "notes": "exact receipt",
        "reviewed_at": "2026-08-20T00:00:00+00:00",
    }

    first = record_review(checkpoint.directory, root, **kwargs)
    second = record_review(checkpoint.directory, root, **kwargs)

    assert first.receipt_path == second.receipt_path
    assert {path.name for path in first.receipt_path.parent.iterdir()} == {
        "receipt.json",
        "receipt.md",
    }


def test_concurrent_identical_review_replay_is_idempotent(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")

    def record(_index: int):
        return record_review(
            checkpoint.directory,
            root,
            reviewer="reviewer",
            disposition="accept",
            notes="identical",
            reviewed_at="2026-08-20T00:00:00+00:00",
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        receipts = list(pool.map(record, range(64)))

    assert {receipt.receipt_path for receipt in receipts} == {
        checkpoint.directory / "reviews" / "reviewer" / "receipt.json"
    }


def test_concurrent_completion_losers_are_normalized(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    record_review(checkpoint.directory, root, reviewer="reviewer", disposition="accept")

    def seal(_index: int) -> str:
        try:
            seal_completion(checkpoint.directory, root, evidence_allowlist=set())
        except CheckpointError as exc:
            return str(exc)
        return "sealed"

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(seal, range(32)))

    assert results.count("sealed") == 1
    assert set(results) <= {"sealed", "completion has already been sealed"}


def test_verify_rejects_forged_escape_and_workspace_symlink(tmp_path) -> None:
    root = _workspace(tmp_path)
    checkpoint = create_checkpoint(root, ["src/app.py"], tmp_path / "checkpoint")
    outside = tmp_path / "outside.txt"
    outside.write_text("print('ok')\n", encoding="utf-8")
    inventory = checkpoint.inventory_path
    manifest_path = checkpoint.directory / "checkpoint.json"
    inventory.chmod(0o644)
    manifest_path.chmod(0o644)
    forged = f"{checkpoint_module._sha256(outside)}  ../outside.txt\n".encode()
    inventory.write_bytes(forged)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inventory_sha256"] = checkpoint_module._sha256_bytes(forged)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CheckpointError, match="canonical"):
        verify_checkpoint(checkpoint.directory, root)

    other = create_checkpoint(root, ["src/app.py"], tmp_path / "other")
    app = root / "src" / "app.py"
    app.unlink()
    app.symlink_to(outside)
    receipt = verify_checkpoint(other.directory, root)
    assert receipt.sealed is False
    assert receipt.missing_paths == ("src/app.py",)


def test_checkpoint_rejects_symlink_output_directory(tmp_path) -> None:
    root = _workspace(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    output = tmp_path / "checkpoint"
    output.symlink_to(outside, target_is_directory=True)

    with pytest.raises(CheckpointError, match="symlink"):
        create_checkpoint(root, ["src/app.py"], output)
