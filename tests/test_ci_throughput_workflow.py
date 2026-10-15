"""Deterministic validation for the governed CI throughput workflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pytest.yml"
TEST_ENV = ROOT / ".github" / "actions" / "test-env" / "action.yml"
CLASSIFIER = ROOT / "scripts" / "ci" / "classify-test-impact.sh"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=path, check=True, capture_output=True, text=True
    ).stdout.strip()


def _classify(path: Path, base: str, head: str) -> str:
    output = path / "result.txt"
    output.unlink(missing_ok=True)
    env = {**os.environ, "GITHUB_OUTPUT": str(output)}
    subprocess.run([CLASSIFIER, base, head], cwd=path, env=env, check=True)
    return output.read_text(encoding="utf-8").strip()


def test_docs_only_classifier_fails_closed(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "CI")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "docs" / "guide.md").write_text("two\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "docs")
    docs_head = _git(tmp_path, "rev-parse", "HEAD")
    assert _classify(tmp_path, base, docs_head) == "docs_only=true"

    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: ci\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "workflow")
    runtime_head = _git(tmp_path, "rev-parse", "HEAD")
    assert _classify(tmp_path, docs_head, runtime_head) == "docs_only=false"

    nested_markdown = tmp_path / "src" / "contract.md"
    nested_markdown.parent.mkdir()
    nested_markdown.write_text("not documentation scope\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "nested markdown")
    nested_head = _git(tmp_path, "rev-parse", "HEAD")
    assert _classify(tmp_path, runtime_head, nested_head) == "docs_only=false"


def test_workflow_preserves_required_checks_and_coverage() -> None:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    unit = data["jobs"]["unit"]
    workflow = WORKFLOW.read_text(encoding="utf-8")

    # Push / schedule / workflow_call keep the 2-version matrix job; the two
    # pull_request required checks are separate jobs with the literal names.
    assert unit["name"] == "unit tests (py${{ matrix.python-version }})"
    assert data["jobs"]["unit-py311"]["name"] == "unit tests (py3.11)"
    assert data["jobs"]["unit-py312"]["name"] == "unit tests (py3.12)"
    assert '["3.11","3.12"]' in workflow
    # The push matrix was deliberately narrowed to match the PR matrix on
    # 2026-09-21. 3.10 cannot install at all (the `all` extra needs
    # skwhisper, which floors at 3.11) and 3.13/3.14 die on pgpy's import
    # of imghdr, removed from the stdlib in 3.13. Re-adding any of them
    # here without first fixing that dependency floor makes main
    # permanently unpublishable, which is exactly what happened: every
    # publish run failed while each PR stayed green.
    assert '["3.10","3.11","3.12","3.13","3.14"]' not in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert "matrix.python-version == '3.12'" in workflow
    assert "scripts/ci/run-python311-compat.sh" in workflow
    assert "run-python311-compat.sh" in workflow
    # The install/verify steps live in the shared composite action so the
    # sharded py3.12 jobs and the py3.11 / push jobs cannot drift.
    assert "./.github/actions/test-env" in workflow
    assert "python -m pip check" in TEST_ENV.read_text(encoding="utf-8")
    assert "uses: ./.github/workflows/pytest.yml" in (
        ROOT / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    # push joined pull_request on 2026-09-21. merge_group stays excluded:
    # a merge-queue entry cancelled mid-run needs GitHub's own queue
    # handling. With push excluded, GitHub held one pending run per group
    # and evicted it on the next push, so every publish run died at
    # `classify test impact` before testing anything.
    assert (
        "cancel-in-progress: ${{ github.event_name == 'pull_request'"
        " || github.event_name == 'push' }}" in workflow
    )
    for job in data["jobs"].values():
        assert not job.get("continue-on-error")
        for step in job.get("steps", []):
            if step.get("continue-on-error"):
                assert step["name"] == "Upload coverage"
                assert step["uses"].startswith("codecov/codecov-action@")
                assert step["with"]["fail_ci_if_error"] is False
                assert "run" not in step
    assert "|| true" not in workflow


def test_py312_pull_request_suite_is_sharded_behind_the_required_check() -> None:
    """`unit tests (py3.12)` stays the required name and aggregates the shards."""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    shard = data["jobs"]["shard"]
    agg = data["jobs"]["unit-py312"]

    count = len(shard["strategy"]["matrix"]["shard"])
    assert shard["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    assert count >= 4
    assert shard["name"] != agg["name"]
    assert f"/{count} (py3.12)" in shard["name"]
    assert "shard" in agg["needs"]
    assert not shard.get("continue-on-error")
    run = next(s["run"] for s in shard["steps"] if s.get("name") == "Run unit shard")
    assert f"--shard ${{{{ matrix.shard }}}}/{count}" in run
    assert "not integration and not e2e" in run
    assert "|| true" not in run
    final = agg["steps"][-1]
    assert final["name"] == "Require every py3.12 shard to pass"
    assert "needs.shard.result" in yaml.dump(final)


def test_py311_required_check_does_not_wait_for_the_shards() -> None:
    """py3.11 runs in parallel with the shards: it may need only `classify`."""
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job = data["jobs"]["unit-py311"]
    needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
    assert needs == ["classify"]
    assert any("run-python311-compat.sh" in (s.get("run") or "") for s in job["steps"])
    # Exactly one job per event carries each required name.
    assert "pull_request" in job["if"]
    assert "!= 'pull_request'" in data["jobs"]["unit"]["if"]
