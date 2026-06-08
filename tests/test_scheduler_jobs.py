"""Tests for skcapstone.scheduler_jobs — JobSpec, YAML loading, node affinity,
due-check, and host-alias discovery.

Each group corresponds to one implementation commit:
  A — JobSpec + load_jobs
  B — job_runs_here (node affinity)
  C — is_due (cron + interval with misfire catch-up)
  D — current_host_aliases
"""

# ---------------------------------------------------------------------------
# Group A — JobSpec + load_jobs
# ---------------------------------------------------------------------------
from pathlib import Path

from skcapstone.scheduler_jobs import JobSpec, load_jobs


def test_load_jobs_parses_yaml(tmp_path: Path):
    cfg = tmp_path / "jobs.yaml"
    cfg.write_text(
        "jobs:\n"
        "  gtd-triage:\n"
        "    schedule: '0 6 * * *'\n"
        "    type: agent\n"
        "    nodes: ['.41']\n"
        "    agent: lumina\n"
        "    prompt: 'triage inbox'\n"
        "    timeout: 900\n"
        "  health:\n"
        "    every: 300s\n"
        "    type: python\n"
        "    nodes: all\n"
        "    callback: skcapstone.service_health:run_once\n",
        encoding="utf-8",
    )
    jobs = load_jobs(cfg)
    by_name = {j.name: j for j in jobs}
    assert by_name["gtd-triage"].schedule == "0 6 * * *"
    assert by_name["gtd-triage"].every_seconds is None
    assert by_name["gtd-triage"].type == "agent"
    assert by_name["gtd-triage"].nodes == [".41"]
    assert by_name["health"].every_seconds == 300.0
    assert by_name["health"].nodes == "all"
    assert by_name["health"].enabled is True


def test_load_jobs_missing_file_returns_empty(tmp_path: Path):
    assert load_jobs(tmp_path / "nope.yaml") == []


# ---------------------------------------------------------------------------
# Group B — node affinity
# ---------------------------------------------------------------------------
from skcapstone.scheduler_jobs import job_runs_here  # noqa: E402


def test_job_runs_here_all():
    assert job_runs_here(JobSpec(name="x", nodes="all"), host_aliases={"hostA", ".41"})


def test_job_runs_here_match_and_miss():
    j = JobSpec(name="x", nodes=[".41"])
    assert job_runs_here(j, host_aliases={".41"})
    assert not job_runs_here(j, host_aliases={".158", "noroc2027"})


# ---------------------------------------------------------------------------
# Group C — due-check cron + interval with misfire catch-up
# ---------------------------------------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from skcapstone.scheduler_jobs import is_due  # noqa: E402


def test_interval_due():
    j = JobSpec(name="x", every_seconds=300)
    now = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)
    assert is_due(j, last_run=None, now=now)
    assert not is_due(j, last_run=now - timedelta(seconds=100), now=now)
    assert is_due(j, last_run=now - timedelta(seconds=301), now=now)


def test_cron_due_at_scheduled_minute():
    j = JobSpec(name="x", schedule="0 6 * * *")
    six_am = datetime(2026, 6, 8, 6, 0, 30, tzinfo=timezone.utc)
    assert is_due(j, last_run=None, now=six_am)
    assert not is_due(j, last_run=six_am, now=six_am + timedelta(minutes=5))
    assert is_due(j, last_run=six_am - timedelta(days=1), now=six_am)


# ---------------------------------------------------------------------------
# Group D — host alias discovery
# ---------------------------------------------------------------------------
import socket  # noqa: E402

from skcapstone.scheduler_jobs import current_host_aliases  # noqa: E402


def test_current_host_aliases_includes_hostname():
    assert socket.gethostname() in current_host_aliases()


def test_current_host_aliases_includes_env_alias(monkeypatch):
    monkeypatch.setenv("SK_NODE_ALIAS", ".41, noroc-test")
    a = current_host_aliases()
    assert ".41" in a and "noroc-test" in a
