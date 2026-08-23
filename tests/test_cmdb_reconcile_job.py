"""Tests for the scheduled CMDB reconciliation entrypoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcoord.cmdb_reconcile import read_verified_run_artifacts
from skcoord.cmdb_scheduler import ScheduledReconcileConfig

from skcapstone.cmdb_reconcile_job import run_cmdb_reconcile_job
from skcapstone.scheduler_jobs import load_jobs


class CannedRunner:
    def __init__(self, host: str) -> None:
        self.host = host

    def run(self, argv) -> str:
        if argv[:1] == ["uname"]:
            return "Linux 6.1\n"
        if argv[:1] == ["nproc"]:
            return "4\n"
        if argv[:2] == ["cat", "/proc/sys/net/ipv4/ip_local_port_range"]:
            return "32768 60999\n"
        return ""


def _write_config(home: Path, **overrides) -> ScheduledReconcileConfig:
    values = {
        "enabled": True,
        "owner_node": "chiap04",
        "agent": "jarvis",
        "targets": ("chiap04",),
        "credential_refs": {"chiap04": "skvault://ssh/cmdb-chiap04"},
        "retry_count": 0,
        "cadence_seconds": 900,
    }
    values.update(overrides)
    config = ScheduledReconcileConfig(**values)
    path = home / "config" / "cmdb-reconcile.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(config.as_dict()), encoding="utf-8")
    return config


def test_missing_config_disables_without_creating_cmdb(tmp_path: Path) -> None:
    result = run_cmdb_reconcile_job(tmp_path, aliases={"chiap04"}, agent="jarvis")
    assert result["outcome"] == "disabled"
    assert not (tmp_path / "cmdb").exists()


def test_wrong_node_does_not_scan(tmp_path: Path) -> None:
    _write_config(tmp_path)
    result = run_cmdb_reconcile_job(tmp_path, aliases={"chiap08"}, agent="jarvis")
    assert result["outcome"] == "wrong_node"
    assert not (tmp_path / "cmdb").exists()


def test_wrong_agent_does_not_scan(tmp_path: Path) -> None:
    _write_config(tmp_path)
    result = run_cmdb_reconcile_job(tmp_path, aliases={"chiap04"}, agent="lumina")
    assert result["outcome"] == "wrong_agent"
    assert not (tmp_path / "cmdb").exists()


def test_bundled_job_is_single_node_tick() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "skcapstone"
        / "defaults"
        / "config"
        / "jobs.d"
        / "cmdb-reconcile.yaml"
    )
    job = load_jobs(path)[0]
    assert job.name == "cmdb-reconcile"
    assert job.nodes == ["chiap04"]
    assert job.every_seconds == 60
    assert job.callback == "skcapstone.cmdb_reconcile_job:run_cmdb_reconcile_job"
    assert job.retries == 0


def test_complete_run_applies_records_health_and_respects_cadence(tmp_path: Path) -> None:
    _write_config(tmp_path)
    now = datetime.now(timezone.utc)
    result = run_cmdb_reconcile_job(
        tmp_path,
        aliases={"chiap04"},
        agent="jarvis",
        runner_factory=lambda host: CannedRunner(host),
        now=now,
    )
    artifacts = read_verified_run_artifacts(tmp_path)

    assert result["outcome"] == "complete"
    assert result["applied"]
    assert len(artifacts) == 1
    assert artifacts[0]["job"]["owner_node"] == "chiap04"
    assert artifacts[0]["job"]["agent"] == "jarvis"
    assert artifacts[0]["collector_health"]["targets"][0]["host"] == "chiap04"
    assert artifacts[0]["config_version"]

    second = run_cmdb_reconcile_job(
        tmp_path,
        aliases={"chiap04"},
        agent="jarvis",
        runner_factory=lambda host: CannedRunner(host),
        now=now + timedelta(minutes=1),
    )
    assert second["outcome"] == "not_due"
    assert len(read_verified_run_artifacts(tmp_path)) == 1


def test_disable_after_run_preserves_existing_data(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    now = datetime.now(timezone.utc)
    run_cmdb_reconcile_job(
        tmp_path,
        aliases={"chiap04"},
        agent="jarvis",
        runner_factory=lambda host: CannedRunner(host),
        now=now,
    )
    artifacts_before = read_verified_run_artifacts(tmp_path)
    disabled = ScheduledReconcileConfig.from_mapping({**config.as_dict(), "enabled": False})
    (tmp_path / "config" / "cmdb-reconcile.json").write_text(
        json.dumps(disabled.as_dict()), encoding="utf-8"
    )

    result = run_cmdb_reconcile_job(
        tmp_path,
        aliases={"chiap04"},
        agent="jarvis",
        runner_factory=lambda host: CannedRunner(host),
        now=now + timedelta(hours=1),
    )

    assert result["outcome"] == "disabled"
    assert read_verified_run_artifacts(tmp_path) == artifacts_before
