"""Operator recovery preserves custody and cannot release another generation."""

import hashlib
import json

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardStore, current_claim_precondition

from skcapstone.coordination import Board, Task
from tests.test_cli_coord_deps import _main


@pytest.fixture
def recovery(tmp_path):
    board = Board(tmp_path / "board")
    board.ensure_dirs()
    board.create_task(Task(id="abc12345", title="[S] interrupted source"))
    board.claim_task("old-worker", "abc12345")
    revision = current_claim_precondition(board.home, "abc12345", "old-worker")
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    artifact = evidence / "partial.patch"
    artifact.write_bytes(b"preserved candidate")
    artifact.chmod(0o600)
    receipt = evidence / "recovery.json"
    data = dict(
        schema="skcapstone.operator-recovery/v1", card_id="abc12345",
        owner="old-worker", claim_revision=revision, controller="test-repair",
        authority="operator-attestation", workers_stopped=True,
        statement="The operator confirms this worker stopped and authorizes takeover.",
        custody=[dict(path=str(artifact), sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())],
    )
    args = ["coord", "release-claim", "abc12345", "--owner", "old-worker",
            "--expected-claim-revision", revision, "--agent", "test-repair",
            "--home", str(board.home), "--recovery-receipt", str(receipt)]
    return board, artifact, receipt, data, args


def invoke(recovery):
    _, _, receipt, data, args = recovery
    receipt.write_text(json.dumps(data))
    receipt.chmod(0o600)
    return CliRunner().invoke(_main(), args)


def test_recovery_binds_receipt_and_preserves_source(recovery):
    board, artifact, receipt, _, args = recovery
    result = invoke(recovery)
    assert result.exit_code == 0, result.output
    releases = [e for e in CardStore(board.home)._read_events("abc12345")
                if e.get("action") == "release_claim"]
    assert len(releases) == 1
    assert releases[0]["recovery_receipt_sha256"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert artifact.read_bytes() == b"preserved candidate"
    replay = CliRunner().invoke(_main(), args)
    assert replay.exit_code == 0, replay.output
    assert "Already released" in replay.output


@pytest.mark.parametrize("field,value", [
    ("workers_stopped", False), ("workers_stopped", "true"),
    ("claim_revision", "new-generation"), ("owner", "other"),
    ("controller", "other"), ("card_id", "abcdef12"),
    ("statement", ""), ("custody", []), ("authority", "stale-heartbeat"),
])
def test_wrong_or_unconfirmed_recovery_cannot_release(recovery, field, value):
    board, _, _, data, _ = recovery
    data[field] = value
    result = invoke(recovery)
    assert result.exit_code != 0
    assert CardStore(board.home).fold("abc12345").owner == "old-worker"


@pytest.mark.parametrize("mutation", ["changed", "public", "symlink"])
def test_unsafe_custody_cannot_release(recovery, mutation):
    board, artifact, _, _, _ = recovery
    if mutation == "changed":
        artifact.write_bytes(b"different source")
    elif mutation == "public":
        artifact.chmod(0o644)
    else:
        target = artifact.with_suffix(".original")
        artifact.rename(target)
        artifact.symlink_to(target)
    result = invoke(recovery)
    assert result.exit_code != 0
    assert CardStore(board.home).fold("abc12345").owner == "old-worker"
