"""Guard rails for CI/publish workflow gating.

Regression tests for coord card 62c567cb: releases must never ship on red
tests. These tests parse the GitHub Actions workflow files and assert the
gating invariants hold:

* publish.yml: the test job is not masked with continue-on-error, and both
  publish jobs depend on the test job WITHOUT `if: always()` (so they only
  run when tests succeed).
* publish.yml: the tag-vs-pyproject version verification step is preserved.
* ci.yml: no `|| true` masking on any test or lint step; the masked test
  job is retired in favor of pytest.yml as the honest required check.
* pytest.yml: exists and runs pytest without any `|| true` masking.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    assert path.exists(), f"missing workflow: {path}"
    return yaml.safe_load(path.read_text())


@pytest.fixture(scope="module")
def publish() -> dict:
    return _load("publish.yml")


@pytest.fixture(scope="module")
def ci() -> dict:
    return _load("ci.yml")


class TestPublishGating:
    def test_test_job_exists(self, publish):
        assert "test" in publish["jobs"], "publish.yml must have a test job"

    def test_test_job_not_masked(self, publish):
        job = publish["jobs"]["test"]
        assert not job.get("continue-on-error"), (
            "publish.yml test job must not set continue-on-error at job level"
        )
        for step in job.get("steps", []):
            assert not step.get("continue-on-error"), (
                f"publish.yml test step masked with continue-on-error: {step}"
            )
            run = step.get("run") or ""
            assert "|| true" not in run, (
                f"publish.yml test step masked with '|| true': {step}"
            )

    @pytest.mark.parametrize("job_name", ["publish-pypi", "publish-npm"])
    def test_publish_jobs_gated_on_test_success(self, publish, job_name):
        job = publish["jobs"][job_name]
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "test" in needs, f"{job_name} must depend on the test job"
        cond = str(job.get("if", ""))
        assert "always()" not in cond, (
            f"{job_name} must not use if: always() (it would publish on red tests)"
        )
        assert "failure()" not in cond and "cancelled()" not in cond, (
            f"{job_name} condition must not run on failed/cancelled tests: {cond}"
        )

    def test_version_verification_preserved(self, publish):
        steps = publish["jobs"]["publish-pypi"]["steps"]
        runs = "\n".join(s.get("run", "") or "" for s in steps)
        assert "pyproject.toml" in runs and "GITHUB_REF#refs/tags/v" in runs, (
            "publish-pypi must keep the tag-vs-pyproject version verification"
        )


class TestCiHonesty:
    def test_no_or_true_masking_anywhere(self, ci):
        for job_name, job in ci["jobs"].items():
            for step in job.get("steps", []):
                run = step.get("run") or ""
                assert "|| true" not in run, (
                    f"ci.yml job '{job_name}' step masked with '|| true': "
                    f"{step.get('name', run[:60])}"
                )

    def test_masked_test_job_retired(self, ci):
        # The old masked test job is retired; pytest.yml is the honest
        # required check. ci.yml keeps only lint (advisory) and build.
        assert "test" not in ci["jobs"], (
            "ci.yml masked test job should be retired in favor of pytest.yml"
        )

    def test_pytest_yml_is_honest_required_check(self):
        wf = _load("pytest.yml")
        job = wf["jobs"]["unit"]
        assert not job.get("continue-on-error")
        for step in job.get("steps", []):
            assert "|| true" not in (step.get("run") or "")
            assert not step.get("continue-on-error")
