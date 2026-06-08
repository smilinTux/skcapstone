"""Tests for `skcapstone scheduler` CLI command group."""

import click
from click.testing import CliRunner
from skcapstone.cli.scheduler_cmd import register_scheduler_commands


def _app(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path))
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "jobs.yaml").write_text(
        "jobs:\n  demo:\n    every: 60s\n    type: shell\n    command: 'echo hi'\n    nodes: all\n",
        encoding="utf-8")

    @click.group()
    def main():
        pass

    register_scheduler_commands(main)
    return main


def test_scheduler_list(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["scheduler", "list"])
    assert res.exit_code == 0 and "demo" in res.output


def test_scheduler_run_now(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["scheduler", "run", "demo"])
    assert res.exit_code == 0 and "hi" in res.output


def test_scheduler_run_unknown_job_errors(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    res = CliRunner().invoke(main, ["scheduler", "run", "nope"])
    assert res.exit_code != 0


def test_scheduler_disable_then_list(tmp_path, monkeypatch):
    main = _app(tmp_path, monkeypatch)
    assert CliRunner().invoke(main, ["scheduler", "disable", "demo"]).exit_code == 0
    # after disable, jobs.yaml should mark it disabled
    import yaml
    data = yaml.safe_load((tmp_path / "config" / "jobs.yaml").read_text())
    assert data["jobs"]["demo"]["enabled"] is False
