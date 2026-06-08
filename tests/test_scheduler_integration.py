"""Integration tests for config-driven jobs in TaskScheduler.

Tests that load_config_jobs and tick_config_jobs work correctly, and that
build_scheduler picks up a jobs.yaml file from the config directory.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.scheduler_jobs import JobSpec
from skcapstone.scheduled_tasks import TaskScheduler


def test_due_config_job_for_this_host_fires(tmp_path: Path):
    """Due jobs are dispatched to a worker thread; assert fired after the thread completes."""
    sched = TaskScheduler(home=tmp_path, stop_event=threading.Event())
    fired = []
    done = threading.Event()
    job = JobSpec(name="j", type="shell", command="true", every_seconds=1, nodes=["hostA"])
    sched.load_config_jobs(jobs=[job], hostname="hostA", host_aliases={"hostA"}, state_root=tmp_path)
    from skcapstone.scheduler_runner import JobResult

    def _run(j):
        fired.append(j.name)
        done.set()
        return JobResult(ok=True)

    sched._job_runner.run = _run  # type: ignore
    sched.tick_config_jobs(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    assert done.wait(2), "job should have run within 2s"
    assert fired == ["j"]


def test_job_not_for_this_host_skipped(tmp_path: Path):
    sched = TaskScheduler(home=tmp_path, stop_event=threading.Event())
    fired = []
    job = JobSpec(name="j", type="shell", command="true", every_seconds=1, nodes=[".41"])
    sched.load_config_jobs(jobs=[job], hostname="hostB", host_aliases={"hostB"}, state_root=tmp_path)
    sched._job_runner.run = lambda j: fired.append(j.name)  # type: ignore
    sched.tick_config_jobs()
    assert fired == []


def test_build_scheduler_loads_jobs_yaml(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"; cfg_dir.mkdir()
    (cfg_dir / "jobs.yaml").write_text(
        "jobs:\n  noop:\n    every: 60s\n    type: shell\n    command: 'true'\n    nodes: all\n",
        encoding="utf-8")
    from skcapstone.scheduled_tasks import build_scheduler
    sched = build_scheduler(home=tmp_path, stop_event=threading.Event())
    assert any(j.name == "noop" for j in sched._config_jobs)


def test_tick_does_not_block_on_slow_job(tmp_path):
    """Verify that tick_config_jobs returns immediately even when a job is slow.

    The job worker runs in a daemon thread; a slow job must not hold up the
    scheduler tick thread (which also drives heartbeats and built-in tasks).
    """
    import time
    from datetime import datetime, timezone

    sched = TaskScheduler(home=tmp_path, stop_event=threading.Event())
    job = JobSpec(name="slow", type="shell", command="true", every_seconds=1, nodes=["h"])
    sched.load_config_jobs(jobs=[job], hostname="h", host_aliases={"h"}, state_root=tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_run(j):
        started.set()
        release.wait(5)
        from skcapstone.scheduler_runner import JobResult
        return JobResult(ok=True)

    sched._job_runner.run = slow_run  # type: ignore

    t0 = time.monotonic()
    sched.tick_config_jobs(now=datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc))
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"tick must not block on the job, but took {elapsed:.3f}s"
    assert started.wait(2), "job worker should have started in background"
    release.set()
