"""Tests for jobs.d/ drop-in registration: load_jobs_with_dropins,
register_job, unregister_job."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from skcapstone.scheduler_jobs import (
    load_jobs_with_dropins,
    register_job,
    unregister_job,
)


def _write_base(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "config" / "jobs.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(body, encoding="utf-8")
    return cfg


def test_merges_base_and_dropins(tmp_path: Path):
    """Base jobs.yaml + a jobs.d fragment both load."""
    cfg = _write_base(tmp_path, "jobs:\n  base_job:\n    every: 5m\n    type: shell\n    command: 'echo base'\n")
    register_job({"name": "svc_job", "every": "15m", "type": "shell", "command": "echo svc"}, home=tmp_path)

    jobs = {j.name: j for j in load_jobs_with_dropins(cfg)}
    assert set(jobs) == {"base_job", "svc_job"}
    assert jobs["svc_job"].every_seconds == 900.0
    assert jobs["base_job"].command == "echo base"


def test_dropin_overrides_base_with_warning(tmp_path: Path):
    """A drop-in defining the same name wins and warns."""
    cfg = _write_base(tmp_path, "jobs:\n  dup:\n    every: 5m\n    type: shell\n    command: 'echo base'\n")
    register_job({"name": "dup", "every": "1h", "type": "shell", "command": "echo override"}, home=tmp_path)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        jobs = {j.name: j for j in load_jobs_with_dropins(cfg)}

    assert jobs["dup"].every_seconds == 3600.0
    assert jobs["dup"].command == "echo override"
    assert any("overrides" in str(w.message) for w in caught)


def test_no_base_file_only_dropins(tmp_path: Path):
    """Drop-ins load even when jobs.yaml does not exist."""
    cfg = tmp_path / "config" / "jobs.yaml"  # never created
    register_job({"name": "lonely", "schedule": "0 * * * *", "type": "shell", "command": "echo hi"}, home=tmp_path)
    jobs = load_jobs_with_dropins(cfg)
    assert [j.name for j in jobs] == ["lonely"]
    assert jobs[0].schedule == "0 * * * *"


def test_register_requires_name(tmp_path: Path):
    with pytest.raises(ValueError):
        register_job({"every": "5m"}, home=tmp_path)


def test_register_requires_schedule_or_every(tmp_path: Path):
    with pytest.raises(ValueError):
        register_job({"name": "bad", "type": "shell", "command": "echo hi"}, home=tmp_path)


def test_register_is_idempotent(tmp_path: Path):
    """Re-registering the same name overwrites, never duplicates."""
    cfg = tmp_path / "config" / "jobs.yaml"
    register_job({"name": "j", "every": "5m", "type": "shell", "command": "v1"}, home=tmp_path)
    register_job({"name": "j", "every": "5m", "type": "shell", "command": "v2"}, home=tmp_path)
    jobs = load_jobs_with_dropins(cfg)
    assert len(jobs) == 1
    assert jobs[0].command == "v2"


def test_unregister(tmp_path: Path):
    cfg = tmp_path / "config" / "jobs.yaml"
    register_job({"name": "j", "every": "5m", "type": "shell", "command": "echo"}, home=tmp_path)
    assert unregister_job("j", home=tmp_path) is True
    assert load_jobs_with_dropins(cfg) == []
    assert unregister_job("j", home=tmp_path) is False
