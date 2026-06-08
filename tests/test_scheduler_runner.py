"""Tests for skcapstone.scheduler_runner — JobRunner execution, overlap lock,
result shapes, and error containment.

Each test targets a specific contract:
  - python jobs call the registered callback
  - shell jobs capture stdout and return ok=True
  - nonzero exit codes produce ok=False with the correct exit_code
  - exceptions in python callbacks are caught and surfaced in result.error
  - the overlap lock prevents a second concurrent acquire
  - unknown job types return ok=False without raising
"""

from pathlib import Path

from skcapstone.scheduler_jobs import JobSpec
from skcapstone.scheduler_runner import JobRunner


def test_python_job_calls_callback(tmp_path: Path):
    called = {}
    import skcapstone.scheduler_runner as sr
    sr._TEST_HOOK = lambda: called.setdefault("hit", True)  # type: ignore
    job = JobSpec(name="t", type="python", callback="skcapstone.scheduler_runner:_TEST_HOOK")
    result = JobRunner(log_dir=tmp_path).run(job)
    assert result.ok and called.get("hit") is True


def test_shell_job_runs_command(tmp_path: Path):
    job = JobSpec(name="echo", type="shell", command="echo hello", timeout=10)
    result = JobRunner(log_dir=tmp_path).run(job)
    assert result.ok and "hello" in result.output


def test_shell_job_nonzero_is_error(tmp_path: Path):
    job = JobSpec(name="fail", type="shell", command="sh -c 'exit 3'", timeout=10)
    result = JobRunner(log_dir=tmp_path).run(job)
    assert not result.ok and result.exit_code == 3


def test_python_job_exception_is_caught(tmp_path: Path):
    import skcapstone.scheduler_runner as sr
    def _boom(): raise RuntimeError("nope")
    sr._TEST_BOOM = _boom  # type: ignore
    job = JobSpec(name="b", type="python", callback="skcapstone.scheduler_runner:_TEST_BOOM")
    result = JobRunner(log_dir=tmp_path).run(job)
    assert not result.ok and "nope" in result.error


def test_overlap_lock_blocks_second_run(tmp_path: Path):
    runner = JobRunner(log_dir=tmp_path)
    job = JobSpec(name="locked", type="shell", command="echo x", timeout=10)
    with runner.lock(job) as got:
        assert got
        with runner.lock(job) as second:
            assert not second
    # lock released after context exit -> can acquire again
    with runner.lock(job) as third:
        assert third


def test_unknown_type_is_error(tmp_path: Path):
    job = JobSpec(name="x", type="weird")
    result = JobRunner(log_dir=tmp_path).run(job)
    assert not result.ok
