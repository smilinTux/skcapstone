"""Guard rails for CI/publish workflow gating.

Release-workflow invariants, parsed from the GitHub Actions workflow files.

publish.yml uses OIDC Trusted Publishing and reuses the honest Python test
workflow before creating a tag or publishing an artifact. The invariants are:

* publish.yml: a `build` job runs `twine check` (artifact integrity gate),
  and `pypi-publish` depends on `build` and publishes via OIDC (the `pypi`
  environment with `id-token: write`, no PyPI token).
* publish.yml: calls pytest.yml and both tag and build wait for it.
* publish.yml: the npm job keeps the tag -> package.json version sync.
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
    """publish.yml: OIDC publishing gated on tests and artifact integrity."""

    def test_build_job_runs_twine_check(self, publish):
        assert "build" in publish["jobs"], "publish.yml must have a build job"
        runs = "\n".join(s.get("run", "") or "" for s in publish["jobs"]["build"].get("steps", []))
        assert (
            "twine check" in runs
        ), "build job must run `twine check` as the artifact-integrity gate"

    def test_pypi_publish_gated_on_build(self, publish):
        job = publish["jobs"]["pypi-publish"]
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else (needs or [])
        assert "build" in needs, "pypi-publish must depend on the build job"

    def test_pypi_publish_uses_oidc_no_token(self, publish):
        job = publish["jobs"]["pypi-publish"]
        assert job.get("environment") == "pypi", "pypi-publish must use the `pypi` environment"
        perms = job.get("permissions") or {}
        assert perms.get("id-token") == "write", "pypi-publish must request id-token: write (OIDC)"
        steps_blob = yaml.safe_dump(job.get("steps", []))
        assert (
            "TWINE_PASSWORD" not in steps_blob and "PYPI_TOKEN" not in steps_blob
        ), "Trusted Publishing must not use a PyPI token"
        uses = " ".join(s.get("uses", "") or "" for s in job.get("steps", []))
        assert (
            "pypa/gh-action-pypi-publish" in uses
        ), "pypi-publish must publish via pypa/gh-action-pypi-publish (OIDC)"

    def test_release_waits_for_python_311_and_312(self, publish):
        tests = publish["jobs"]["tests"]
        assert tests["uses"] == "./.github/workflows/pytest.yml"
        for name in ("tag", "build"):
            needs = publish["jobs"][name].get("needs")
            needs = [needs] if isinstance(needs, str) else (needs or [])
            assert "tests" in needs, f"{name} must wait for Python 3.11 and 3.12"

        build_condition = publish["jobs"]["build"].get("if", "")
        assert "needs.tests.result == 'success'" in build_condition

    def test_pytest_workflow_is_reusable_and_covers_supported_pythons(self):
        workflow = _load("pytest.yml")
        # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
        assert "workflow_call" in workflow[True]
        versions = workflow["jobs"]["unit"]["strategy"]["matrix"]["python-version"]
        assert versions == ["3.11", "3.12"]

    def test_npm_publish_job_is_retired(self, publish):
        """The npm publish job was dropped; skcapstone is Python-first.

        This test used to assert publish-npm kept a tag -> package.json version
        sync. That job was removed in "fix(ci): drop the broken npm publish job",
        so the old assertion failed with KeyError on main. Pin the retirement
        instead, so re-adding npm publishing is a deliberate act that updates
        this test rather than a silent resurrection.
        """
        assert "publish-npm" not in publish["jobs"], (
            "publish-npm was retired; if it is coming back, restore the "
            "tag -> package.json version sync assertion with it"
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
        assert (
            "test" not in ci["jobs"]
        ), "ci.yml masked test job should be retired in favor of pytest.yml"

    def test_pytest_yml_is_honest_required_check(self):
        wf = _load("pytest.yml")
        job = wf["jobs"]["unit"]
        assert not job.get("continue-on-error")
        for step in job.get("steps", []):
            assert "|| true" not in (step.get("run") or "")
            assert not step.get("continue-on-error")
