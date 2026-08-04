"""Guard rails for CI/publish workflow gating.

Release-workflow invariants, parsed from the GitHub Actions workflow files.

publish.yml uses OIDC Trusted Publishing, decoupled from the test gate
(PR #83 / fe3ed56, by Chef): the token-based, test-gated PyPI job was
intentionally replaced with the canonical OIDC flow so a red/flaky unrelated
test can no longer block a tagged release. The invariants now are:

* publish.yml: a `build` job runs `twine check` (artifact integrity gate),
  and `pypi-publish` depends on `build` and publishes via OIDC (the `pypi`
  environment with `id-token: write`, no PyPI token).
* publish.yml: has NO `test` job - PR-time tests live in ci.yml + pytest.yml,
  not the release path.
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
    """publish.yml: OIDC Trusted Publishing, gated on `build`, not on tests.

    PR #83 (fe3ed56) intentionally decoupled the release path from the full
    test suite. These invariants pin the replacement design so it cannot
    silently regress to a token-based or red-test-blocked publish.
    """

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

    def test_release_path_not_gated_on_unrelated_tests(self, publish):
        # PR #83 intent: a red/flaky unrelated test must never block a release.
        # PR-time tests live in ci.yml + pytest.yml, not the release path.
        assert "test" not in publish["jobs"], (
            "publish.yml intentionally has NO test job (PR #83); the release path "
            "is gated on `build`/`twine check`, not the full suite"
        )

    def test_npm_version_sync_preserved(self, publish):
        steps = publish["jobs"]["publish-npm"]["steps"]
        runs = "\n".join(s.get("run", "") or "" for s in steps)
        assert (
            "GITHUB_REF#refs/tags/v" in runs
        ), "publish-npm must keep the tag -> package.json version sync"


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
