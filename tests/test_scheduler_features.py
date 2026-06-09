"""Tests for skscheduler reliability features (2026-06-09):
retries + backoff, jitter parsing, and the notify (sk-alert) hook."""

import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from skcapstone import scheduled_tasks as st
from skcapstone.scheduled_tasks import TaskScheduler
from skcapstone.scheduler_jobs import JobSpec, load_jobs


# --- JobSpec / loader -------------------------------------------------------

def test_load_jobs_parses_reliability_fields(tmp_path: Path):
    cfg = tmp_path / "jobs.yaml"
    cfg.write_text(
        "jobs:\n"
        "  flaky:\n"
        "    every: 60s\n"
        "    type: shell\n"
        "    command: 'true'\n"
        "    retries: 3\n"
        "    retry_backoff: 2.5\n"
        "    jitter: 30\n"
        "    notify: on_failure\n"
        "    notify_level: crit\n"
    )
    (job,) = load_jobs(cfg)
    assert job.retries == 3
    assert job.retry_backoff == 2.5
    assert job.jitter == 30.0
    assert job.notify == "on_failure"
    assert job.notify_level == "crit"


def test_jobspec_reliability_defaults():
    j = JobSpec(name="x")
    assert j.retries == 0 and j.retry_backoff == 0.0 and j.jitter == 0.0
    assert j.notify == "off" and j.notify_level == "warn"


# --- notify hook ------------------------------------------------------------

class _Res:
    def __init__(self, ok, output="", error=""):
        self.ok, self.output, self.error = ok, output, error


def _patch_alert(monkeypatch):
    calls = []
    monkeypatch.setattr(st.shutil, "which", lambda _n: "/usr/bin/sk-alert")
    monkeypatch.setattr(st.subprocess, "run", lambda args, **k: calls.append(args) or None)
    return calls


def test_notify_off_sends_nothing(monkeypatch):
    calls = _patch_alert(monkeypatch)
    TaskScheduler._maybe_notify(JobSpec(name="j", notify="off"), _Res(False, error="boom"), 1)
    assert calls == []


def test_notify_on_failure_fires_with_tail(monkeypatch):
    calls = _patch_alert(monkeypatch)
    TaskScheduler._maybe_notify(
        JobSpec(name="j", notify="on_failure", notify_level="crit"),
        _Res(False, output="line1\nline2", error="boom"), attempts=2)
    assert len(calls) == 1
    args = calls[0]
    assert "-l" in args and "crit" in args
    assert "❌ FAILED" in args[-1] and "after 2 attempts" in args[-1]
    assert "line2" in args[-1]


def test_notify_on_success_skips_failure(monkeypatch):
    calls = _patch_alert(monkeypatch)
    TaskScheduler._maybe_notify(JobSpec(name="j", notify="on_success"), _Res(False, error="x"), 1)
    assert calls == []


def test_notify_always_fires_on_success_info_level(monkeypatch):
    calls = _patch_alert(monkeypatch)
    TaskScheduler._maybe_notify(JobSpec(name="j", notify="always"), _Res(True, output="done"), 1)
    assert len(calls) == 1 and "info" in calls[0] and "✅ ok" in calls[0][-1]


# --- retries ----------------------------------------------------------------

def test_run_config_job_retries_until_success(monkeypatch):
    seq = [_Res(False, error="e1"), _Res(False, error="e2"), _Res(True)]
    n = {"runs": 0}
    recorded = {}

    class FakeRunner:
        @contextmanager
        def lock(self, job):
            yield True
        def run(self, job):
            r = seq[n["runs"]]; n["runs"] += 1; return r

    class FakeState:
        def record_run(self, name, now, ok, error):
            recorded.update(ok=ok, error=error, name=name)

    mgr = TaskScheduler(home=Path("/tmp"), stop_event=threading.Event())
    mgr._job_runner = FakeRunner()
    mgr._state = FakeState()
    job = JobSpec(name="flaky", retries=2, retry_backoff=0.0, jitter=0.0)
    mgr._run_config_job(job, datetime.now(timezone.utc))
    assert n["runs"] == 3          # failed twice, succeeded on 3rd
    assert recorded["ok"] is True


def test_run_config_job_stops_after_exhausting_retries(monkeypatch):
    n = {"runs": 0}; recorded = {}

    class FakeRunner:
        @contextmanager
        def lock(self, job):
            yield True
        def run(self, job):
            n["runs"] += 1; return _Res(False, error="always fails")

    class FakeState:
        def record_run(self, name, now, ok, error):
            recorded.update(ok=ok)

    mgr = TaskScheduler(home=Path("/tmp"), stop_event=threading.Event())
    mgr._job_runner = FakeRunner(); mgr._state = FakeState()
    mgr._run_config_job(JobSpec(name="bad", retries=1), datetime.now(timezone.utc))
    assert n["runs"] == 2          # 1 + 1 retry
    assert recorded["ok"] is False
