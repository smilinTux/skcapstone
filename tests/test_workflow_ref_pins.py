"""Every workflow ref resolves, or this suite goes red instead of absent.

This test is deliberately placed in the pytest suite rather than only in
docs-check. On 2026-09-19 a broken `uses:` ref in `.github/workflows/docs-check.yml`
made that workflow fail BEFORE creating a job, which publishes no check run at
all: the required gate went ABSENT, not red, and every consumer read the PR as
healthy. A guard that lives inside docs-check cannot observe a broken
docs-check. `pytest.yml` has no cross-repo `uses:` of its own, so it still runs
when a cross-repo ref is broken, which makes it a valid vantage point.

The network half (does the pinned sha still resolve upstream) runs in the
`shim-imports` job, not here: a unit suite that reaches the GitHub API would be
flaky and would fail closed for the wrong reason. Here we assert only what is
decidable offline, which is exactly the abbreviated-sha and unpinned-ref class.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GUARD = PROJECT_ROOT / "scripts" / "ci" / "workflow_refs.py"


def _load():
    spec = importlib.util.spec_from_file_location("workflow_refs", GUARD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def guard():
    assert GUARD.is_file(), f"{GUARD} is missing; the ref guard has been removed"
    return _load()


def test_repo_workflow_refs_are_all_well_formed(guard):
    """The live repo: no abbreviated shas, every reusable workflow sha-pinned."""
    pins, findings = guard.collect(PROJECT_ROOT)
    failures = [f for f in findings if f.level == "fail"]
    assert not failures, "\n".join(f.render() for f in failures)


def test_there_is_something_to_check(guard):
    """Zero workflows parsed would make the assertion above vacuously true.

    This is the same defect the guard exists to prevent, one level up: a test
    that passes because it observed nothing is not a passing test.
    """
    assert guard.workflow_files(PROJECT_ROOT), "no workflow files found -- nothing was checked"


def test_guard_catches_an_abbreviated_reusable_workflow_sha(guard, tmp_path):
    """The 2026-09-19 09:29Z break, reproduced."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9f\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "ABBREVIATED" in f.msg for f in findings)


def test_pytest_sibling_dependencies_are_immutable_and_reviewed():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "pytest.yml").read_text(
        encoding="utf-8"
    )
    assert "git ls-remote" not in workflow
    for package in ("skcoord", "skdashboard", "skharness"):
        matching = [line for line in workflow.splitlines() if f'"{package} @ git+' in line]
        assert len(matching) == 1, f"expected one immutable pin for {package}"
        revision = matching[0].rsplit("@", 1)[-1].split('"', 1)[0]
        assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision)


def test_guard_catches_a_mutable_branch_pin(guard, tmp_path):
    """A branch pin is failure mode 1 waiting to happen: delete it and the gate
    goes absent rather than red."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@main\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "full 40-hex sha" in f.msg for f in findings)


def test_guard_catches_an_abbreviated_ref_shaped_input(guard, tmp_path):
    """`standards-ref` selects which validator actually runs and drifts
    independently of the `uses:` pin, so it needs the same rule."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n"
        "    with:\n      standards-ref: 8a799322af9f\n",
        encoding="utf-8",
    )
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "standards-ref" in f.msg for f in findings)


def test_guard_does_not_flag_a_correct_full_sha_pin(guard, tmp_path):
    """Positive control. A guard that flags everything is as useless as one that
    flags nothing."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs-check.yml").write_text(
        "jobs:\n  docs:\n    uses: smilinTux/sk-standards/.github/workflows/"
        "docs-check.yml@8a799322af9fb6b6b765988d3a8683c6761f7195\n",
        encoding="utf-8",
    )
    pins, findings = guard.collect(tmp_path)
    assert not findings
    assert len(pins) == 1


def test_unparseable_workflow_is_a_failure_not_a_skip(guard, tmp_path):
    """A workflow we could not read is a workflow we did not check. Reporting
    that as clean is the CardStore.fold defect in miniature."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "broken.yml").write_text("jobs:\n  docs:\n   uses: [\n", encoding="utf-8")
    _, findings = guard.collect(tmp_path)
    assert any(f.level == "fail" and "could not parse" in f.msg for f in findings)


def test_guard_self_test_passes():
    """Run the script's own negative control the way CI would."""
    r = subprocess.run(
        [sys.executable, str(GUARD), "--self-test"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stdout + r.stderr
