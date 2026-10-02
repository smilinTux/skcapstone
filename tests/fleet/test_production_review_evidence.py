"""Committed review proposals preserve original source identity and custody."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from skcapstone.fleet.production_review_evidence import ReviewEvidenceError, inspect_proposal


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def proposal(tmp_path):
    workspace = tmp_path / "review"
    workspace.mkdir()
    git(workspace, "init", "-q", "-b", "work/review")
    git(workspace, "config", "user.name", "Review Fixture")
    git(workspace, "config", "user.email", "review@example.invalid")
    (workspace / "source.py").write_text("answer = 42\n")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "unpublished source")
    binding = dict(
        card="ab000002",
        parent_card="ab000001",
        source_head=git(workspace, "rev-parse", "HEAD"),
        source_tree=git(workspace, "rev-parse", "HEAD^{tree}"),
        reviewer_identity="pi-seraph-chiap08-ab000002",
    )
    directory = workspace / "docs/evidence/agents/ab000002"
    directory.mkdir(parents=True)
    report = directory / "COMPLETION-EVIDENCE.md"
    report.write_text("Independent source review. Required tests are controller verified.\n")
    report.chmod(0o600)
    decision = dict(
        schema="skfleet.source-review-decision/v1",
        **binding,
        verdict="PASS",
        report_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
    )
    path = directory / "REVIEW-DECISION.json"
    path.write_text(json.dumps(decision))
    path.chmod(0o600)
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "review evidence only")
    return workspace, binding, path, report


def test_real_git_review_commit_preserves_candidate_and_reads_committed_proposal(proposal):
    workspace, binding, path, report = proposal
    result = inspect_proposal(workspace, **binding)
    assert result["proposal"]["source_head"] == binding["source_head"]
    assert result["review_head"] != binding["source_head"]
    assert result["review_head"] == git(workspace, "rev-parse", "HEAD")
    assert result["report_sha256"] == hashlib.sha256(report.read_bytes()).hexdigest()
    assert result["decision_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert git(workspace, "status", "--porcelain") == ""
    assert inspect_proposal(workspace, **binding) == result


def test_review_inspection_through_hardened_coordinator_boundary(proposal):
    workspace, binding, _, _ = proposal
    source = Path(__file__).resolve().parents[2] / "src"
    with tempfile.TemporaryDirectory(prefix="skfleet-review-test-", dir=Path.home()) as tmp:
        # A worker checkout lives outside the coordinator's private /tmp.
        retained = Path(tmp) / "review"
        shutil.copytree(workspace, retained)
        unit = "skfleet-inspection-test-" + uuid.uuid4().hex + ".service"
        program = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from skcapstone.fleet.production_review_evidence import inspect_proposal
result=inspect_proposal(Path(sys.argv[2]), **json.loads(sys.argv[3]))
assert 'NoNewPrivs:\\t1' in Path('/proc/self/status').read_text()
print(json.dumps({'head':result['review_head'], 'verdict':result['proposal']['verdict']}))
"""
        try:
            result = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--quiet",
                    "--wait",
                    "--pipe",
                    "--collect",
                    "--unit=" + unit,
                    "--property=NoNewPrivileges=yes",
                    "--property=PrivateTmp=yes",
                    "--property=RuntimeMaxSec=50",
                    "--property=KillMode=control-group",
                    "--",
                    sys.executable,
                    "-c",
                    program,
                    str(source),
                    str(retained),
                    json.dumps(binding),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert result.returncode == 0, result.stderr
            assert json.loads(result.stdout) == {
                "head": git(workspace, "rev-parse", "HEAD"),
                "verdict": "PASS",
            }
        finally:
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=5)


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_head", "a" * 40),
        ("source_tree", "b" * 40),
        ("parent_card", "ab000003"),
        ("card", "ab000004"),
        ("reviewer_identity", "someone-else"),
        ("report_sha256", "0" * 64),
        ("verdict", "SUCCESS"),
        ("schema", "unversioned"),
    ],
)
def test_committed_mismatched_decision_is_refused(proposal, key, value):
    workspace, binding, path, _ = proposal
    body = json.loads(path.read_text())
    body[key] = value
    path.write_text(json.dumps(body))
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "wrong review binding")
    with pytest.raises(ReviewEvidenceError):
        inspect_proposal(workspace, **binding)


@pytest.mark.parametrize("kind", ["dirty", "source-edit", "duplicate-json-key", "symlink", "fifo"])
def test_artifact_and_git_custody_refusals(proposal, kind):
    workspace, binding, path, report = proposal
    if kind == "dirty":
        report.write_text("uncommitted change")
    elif kind == "source-edit":
        (workspace / "source.py").write_text("answer = 0\n")
        git(workspace, "add", ".")
        git(workspace, "commit", "-qm", "reviewer changed producer source")
    elif kind == "duplicate-json-key":
        path.write_text(path.read_text()[:-1] + ',"verdict":"PASS"}')
        git(workspace, "add", ".")
        git(workspace, "commit", "-qm", "duplicate key")
    elif kind == "symlink":
        report.unlink()
        report.symlink_to(path)
    else:
        report.unlink()
        os.mkfifo(report)
    with pytest.raises(ReviewEvidenceError):
        inspect_proposal(workspace, **binding)


@pytest.mark.parametrize("verdict", ["FAIL", "BLOCKED"])
def test_nonpass_proposals_are_retained_without_becoming_acceptance(proposal, verdict):
    workspace, binding, path, _ = proposal
    body = json.loads(path.read_text())
    body["verdict"] = verdict
    path.write_text(json.dumps(body))
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "nonpass review")
    assert inspect_proposal(workspace, **binding)["proposal"]["verdict"] == verdict
