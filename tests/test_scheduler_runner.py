"""Tests for skcapstone.scheduler_runner - JobRunner execution, overlap lock,
result shapes, and error containment.

Each test targets a specific contract:
  - python jobs call the registered callback
  - shell jobs capture stdout and return ok=True
  - nonzero exit codes produce ok=False with the correct exit_code
  - exceptions in python callbacks are caught and surfaced in result.error
  - the overlap lock prevents a second concurrent acquire
  - unknown job types return ok=False without raising
"""

import json
import os
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

    def _boom():
        raise RuntimeError("nope")

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


def test_lock_pid_reuse_is_stale(tmp_path: Path, monkeypatch):
    """A live replacement process with the same PID must not hold the lock."""
    lock = tmp_path / "reused.lock"
    lock.write_text(json.dumps({"v": 1, "pid": 4242, "btime": 11,
                               "token": "00000000-0000-0000-0000-000000000001"}))
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "gone"))
    assert JobRunner._classify_lock(lock) == JobRunner._STALE


def test_lock_live_owner_remains_excluded(tmp_path: Path, monkeypatch):
    lock = tmp_path / "live.lock"
    lock.write_text(json.dumps({"v": 1, "pid": os.getpid(), "btime": 11,
                               "token": "00000000-0000-0000-0000-000000000001"}))
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "alive"))
    assert JobRunner._classify_lock(lock) == JobRunner._LIVE
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="live", type="shell")) as acquired:
        assert not acquired


def test_lock_reclaims_crashed_owner(tmp_path: Path, monkeypatch):
    lock = tmp_path / "crashed.lock"
    lock.write_text(json.dumps({"v": 1, "pid": 4242, "btime": 11,
                               "token": "00000000-0000-0000-0000-000000000001"}))
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "gone"))
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="crashed", type="shell")) as acquired:
        assert acquired
