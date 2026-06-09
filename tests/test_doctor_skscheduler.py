"""Doctor checks added for sync-conflicts and the skscheduler."""
from pathlib import Path

from skcapstone.doctor import _check_scheduler, _check_sync_conflicts


def test_sync_conflicts_clean(tmp_path: Path):
    checks = _check_sync_conflicts(tmp_path)
    assert len(checks) == 1 and checks[0].passed


def test_sync_conflicts_detected(tmp_path: Path):
    d = tmp_path / "coordination" / "itil" / "incidents"
    d.mkdir(parents=True)
    (d / "inc-x.sync-conflict-20260101-000000-ABC.json").write_text("{}")
    checks = _check_sync_conflicts(tmp_path)
    assert len(checks) == 1 and not checks[0].passed
    assert "1 conflict" in checks[0].detail
    assert "coordination" in checks[0].detail


def test_sync_conflicts_ignores_stversions(tmp_path: Path):
    d = tmp_path / ".stversions"
    d.mkdir()
    (d / "old.sync-conflict-20260101-000000-ABC.json").write_text("{}")
    assert _check_sync_conflicts(tmp_path)[0].passed


def test_scheduler_no_config_is_ok(tmp_path: Path):
    cfg_check = next(c for c in _check_scheduler(tmp_path) if c.name == "scheduler:config")
    assert cfg_check.passed and "not configured" in cfg_check.detail


def test_scheduler_valid_jobs_yaml(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "jobs.yaml").write_text(
        "jobs:\n  j:\n    every: 60s\n    type: shell\n    command: 'true'\n    nodes: all\n",
        encoding="utf-8",
    )
    cfg_check = next(c for c in _check_scheduler(tmp_path) if c.name == "scheduler:config")
    assert cfg_check.passed and "1 job" in cfg_check.detail


def test_scheduler_invalid_jobs_yaml_flagged(tmp_path: Path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "jobs.yaml").write_text("jobs: [this is: not valid mapping", encoding="utf-8")
    cfg_check = next(c for c in _check_scheduler(tmp_path) if c.name == "scheduler:config")
    assert not cfg_check.passed
