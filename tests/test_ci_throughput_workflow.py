"""Deterministic validation for the governed CI throughput workflow."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "pytest.yml"
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

    assert unit["name"] == "unit tests (py${{ matrix.python-version }})"
    assert '["3.11","3.12"]' in workflow
    assert '["3.10","3.11","3.12","3.13","3.14"]' in workflow
    assert "github.event_name != 'pull_request'" in workflow
    assert "matrix.python-version == '3.12'" in workflow
    assert "run-python311-compat.sh" in workflow
    assert "python -m pip check" in workflow
    assert "uses: ./.github/workflows/pytest.yml" in (
        ROOT / ".github" / "workflows" / "publish.yml"
    ).read_text(encoding="utf-8")
    assert "cache-dependency-path: pyproject.toml" in workflow
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert "continue-on-error" not in workflow
    assert "|| true" not in workflow
