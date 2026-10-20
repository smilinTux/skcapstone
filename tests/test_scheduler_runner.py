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
    lock.write_text(
        json.dumps(
            {"v": 1, "pid": 4242, "btime": 11, "token": "00000000-0000-0000-0000-000000000001"}
        )
    )
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "gone"))
    assert JobRunner._classify_lock(lock) == JobRunner._STALE


def test_lock_live_owner_remains_excluded(tmp_path: Path, monkeypatch):
    lock = tmp_path / "live.lock"
    lock.write_text(
        json.dumps(
            {
                "v": 1,
                "pid": os.getpid(),
                "btime": 11,
                "token": "00000000-0000-0000-0000-000000000001",
            }
        )
    )
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "alive"))
    assert JobRunner._classify_lock(lock) == JobRunner._LIVE
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="live", type="shell")) as acquired:
        assert not acquired


def test_lock_reclaims_crashed_owner(tmp_path: Path, monkeypatch):
    lock = tmp_path / "crashed.lock"
    lock.write_text(
        json.dumps(
            {"v": 1, "pid": 4242, "btime": 11, "token": "00000000-0000-0000-0000-000000000001"}
        )
    )
    monkeypatch.setattr(JobRunner, "_owner_state", staticmethod(lambda pid, btime: "gone"))
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="crashed", type="shell")) as acquired:
        assert acquired


# ---------------------------------------------------------------------------
# PID-reuse lock hardening: real /proc, malformed records, release by identity
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
import sys  # noqa: E402

import pytest  # noqa: E402

_TOKEN = "00000000-0000-0000-0000-000000000001"


def _write_lock(path: Path, **fields) -> None:
    path.write_text(json.dumps({"v": 1, "token": _TOKEN, **fields}))


def _age(path: Path, seconds: float) -> None:
    import time

    old = time.time() - seconds
    os.utime(path, (old, old))


def test_real_proc_same_pid_and_boot_time_is_live(tmp_path: Path):
    lock = tmp_path / "a.lock"
    _write_lock(lock, pid=os.getpid(), btime=JobRunner._own_boot_time())
    assert JobRunner._own_boot_time() > 0
    assert JobRunner._classify_lock(lock) == JobRunner._LIVE
    with JobRunner(tmp_path).lock(JobSpec(name="a", type="shell")) as got:
        assert not got
    assert lock.exists(), "a live owner's lock must never be unlinked"


def test_real_proc_pid_reuse_different_boot_time_is_stale(tmp_path: Path):
    """The PID is alive (it is us) but the recorded start time is another process's."""
    lock = tmp_path / "b.lock"
    _write_lock(lock, pid=os.getpid(), btime=JobRunner._own_boot_time() + 12345)
    assert JobRunner._classify_lock(lock) == JobRunner._STALE
    with JobRunner(tmp_path).lock(JobSpec(name="b", type="shell")) as got:
        assert got


def test_real_dead_pid_is_stale(tmp_path: Path):
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    lock = tmp_path / "c.lock"
    _write_lock(lock, pid=proc.pid, btime=7)
    assert JobRunner._classify_lock(lock) == JobRunner._STALE


@pytest.mark.parametrize(
    "fields",
    [
        {"pid": "1", "btime": 5},  # wrong type: str pid
        {"pid": True, "btime": 5},  # bool is an int subclass, must not be taken as pid 1
        {"pid": os.getpid(), "btime": True},
        {"pid": os.getpid(), "btime": 0},  # unverifiable boot time
        {"pid": -1, "btime": 5},
        {"btime": 5},  # missing pid
    ],
)
def test_malformed_v1_record_is_unknown_not_stolen_while_fresh(tmp_path: Path, fields):
    lock = tmp_path / "d.lock"
    _write_lock(lock, **fields)
    assert JobRunner._classify_lock(lock) == JobRunner._UNKNOWN
    with JobRunner(tmp_path).lock(JobSpec(name="d", type="shell")) as got:
        assert not got
    assert lock.exists()
    _age(lock, JobRunner.STALE_LOCK_MAX_AGE_SECONDS + 5)
    assert JobRunner._classify_lock(lock) == JobRunner._STALE


def test_invalid_token_is_not_a_verified_record(tmp_path: Path):
    lock = tmp_path / "e.lock"
    lock.write_text(
        json.dumps({"v": 1, "pid": os.getpid(), "btime": JobRunner._own_boot_time(), "token": "x"})
    )
    assert JobRunner._classify_lock(lock) == JobRunner._UNKNOWN


def test_legacy_pid_only_lock(tmp_path: Path):
    lock = tmp_path / "f.lock"
    lock.write_text(str(os.getpid()))  # live owner, identity unverifiable
    assert JobRunner._classify_lock(lock) == JobRunner._UNKNOWN
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    lock.write_text(str(dead.pid))
    assert JobRunner._classify_lock(lock) == JobRunner._STALE


@pytest.mark.parametrize("junk", [b"", b"\xff\xfe\x00", b"not json", b"[]", b'{"v": 2}', b"null"])
def test_garbage_lock_is_blocked_until_window_then_recovered(tmp_path: Path, junk: bytes):
    lock = tmp_path / "g.lock"
    lock.write_bytes(junk)
    assert JobRunner._classify_lock(lock) == JobRunner._UNKNOWN
    _age(lock, JobRunner.STALE_LOCK_MAX_AGE_SECONDS + 5)
    assert JobRunner._classify_lock(lock) == JobRunner._STALE
    with JobRunner(tmp_path).lock(JobSpec(name="g", type="shell")) as got:
        assert got


def test_lock_is_a_json_record_and_released_on_exit(tmp_path: Path):
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="h", type="shell")) as got:
        assert got
        record = json.loads((tmp_path / "h.lock").read_text())
        assert record["v"] == 1 and record["pid"] == os.getpid()
    assert not (tmp_path / "h.lock").exists()


def test_release_does_not_delete_a_lock_taken_over_by_someone_else(tmp_path: Path):
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="i", type="shell")) as got:
        assert got
        (tmp_path / "i.lock").write_text("new owner")
    assert (tmp_path / "i.lock").read_text() == "new owner"


def test_lock_is_scoped_per_job(tmp_path: Path):
    runner = JobRunner(tmp_path)
    with runner.lock(JobSpec(name="j1", type="shell")) as first:
        with runner.lock(JobSpec(name="j2", type="shell")) as other:
            assert first and other
