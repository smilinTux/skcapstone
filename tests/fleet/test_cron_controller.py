"""Tests for CronController: read-time rows and the MissedRun condition."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import cron, cron_controller, store
from skcapstone.fleet.cli import fleet


@pytest.fixture
def noded41():
    from skcapstone.fleet.store import Writer

    return Writer(role="sknoded", node="node-41", identity="")


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def _cron(paths, operator, name="backup", **spec_kw) -> dict:
    spec = {"command": "backup.sh", "schedule": "@hourly"}
    spec.update(spec_kw)
    return store.write_spec(paths, "cronjob", name, spec, writer=operator)


def test_cron_rows_enabled_disabled_and_skips_deleted(paths, operator) -> None:
    _cron(paths, operator, "backup")
    _cron(paths, operator, "sleeper", enabled=False)
    _cron(paths, operator, "gone", deleted=True)
    rows = {r.name: r for r in cron_controller.cron_rows(paths, "2026-07-27T12:00:00Z")}
    assert set(rows) == {"backup", "sleeper"}
    assert rows["backup"].enabled is True
    assert rows["backup"].schedule == "@hourly"
    assert rows["backup"].node is None
    assert rows["sleeper"].enabled is False


def test_cron_rows_missed_true_past_grace_false_otherwise(paths, operator, noded41) -> None:
    _cron(paths, operator, "backup")
    store.write_status(
        paths,
        "cronjob",
        "backup",
        node="node-41",
        status={"lastRun": "2026-07-27T10:00:00Z"},
        conditions=[],
        observed_generation=1,
        writer=noded41,
    )
    # period 3600s + default grace 60s = 3660s
    just_inside = cron_controller.cron_rows(paths, "2026-07-27T11:00:30Z")[0]
    assert just_inside.missed is False
    assert just_inside.last_run == "2026-07-27T10:00:00Z"
    past_grace = cron_controller.cron_rows(paths, "2026-07-27T11:01:01Z")[0]
    assert past_grace.missed is True
    assert past_grace.next_run == cron.next_run("@hourly", "2026-07-27T10:00:00Z")


def test_cron_rows_baseline_falls_back_to_spec_when_no_status(paths, operator) -> None:
    payload = _cron(paths, operator, "backup")
    created = payload["updatedAt"]
    row = cron_controller.cron_rows(paths, created)[0]
    assert row.last_run is None
    assert row.missed is False  # zero elapsed since the fallback baseline
    assert row.next_run == cron.next_run("@hourly", created)


def test_cronjob_conditions_missed_true_and_false(paths) -> None:
    spec = cron.normalize_cronjob_spec({"command": "x", "schedule": "@hourly"})
    status = {"lastRun": "2026-07-27T10:00:00Z"}
    active = cron_controller.cronjob_conditions(spec, status, "2026-07-27T11:01:01Z")
    assert len(active) == 1
    assert active[0]["type"] == "MissedRun"
    assert active[0]["status"] == "True"
    quiet = cron_controller.cronjob_conditions(spec, status, "2026-07-27T11:00:30Z")
    assert quiet[0]["status"] == "False"


def test_cli_get_cronjobs_lists_columns(paths, operator) -> None:
    _cron(paths, operator, "backup")
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "cronjobs"], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "NAME" in out.output and "SCHEDULE" in out.output and "MISSED" in out.output
    assert "backup" in out.output and "@hourly" in out.output


def test_cli_get_cronjobs_empty(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "cronjobs"], env=_env(paths))
    assert out.exit_code == 0
    assert "no cronjobs" in out.output


def test_cli_describe_cronjob(paths, operator) -> None:
    _cron(paths, operator, "backup")
    runner = CliRunner()
    out = runner.invoke(fleet, ["describe", "cronjob", "backup"], env=_env(paths))
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "backup"
    assert payload["spec"]["spec"]["schedule"] == "@hourly"
