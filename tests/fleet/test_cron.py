"""Tests for the CronJob spec model and pure time helpers."""

from __future__ import annotations

import pytest

from skcapstone.fleet import cron
from skcapstone.fleet.explain import explain


@pytest.mark.parametrize("fraction", ["", ".5", ".555894"])
def test_status_utc_timestamp_precision(fraction: str) -> None:
    baseline = f"2026-10-03T10:15:31{fraction}Z"
    assert cron.next_run("15m", baseline) == "2026-10-03T10:30:31Z"
    assert not cron.is_missed("15m", baseline, "2026-10-03T10:31:31Z")
    assert cron.is_missed("15m", baseline, "2026-10-03T10:31:32Z")


@pytest.mark.parametrize("value", ["invalid", "2026-10-03", "2026-10-03T10:15:31.Z"])
def test_invalid_status_timestamp_still_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        cron.next_run("15m", value)


def test_normalize_valid_spec_applies_defaults() -> None:
    spec = cron.normalize_cronjob_spec({"command": "echo hi", "schedule": "@daily"})
    assert spec == {
        "command": "echo hi",
        "schedule": "@daily",
        "enabled": True,
        "nodeSelector": {},
        "deleted": False,
    }


def test_normalize_echoes_command_and_schedule() -> None:
    spec = cron.normalize_cronjob_spec({"command": "sync-backup", "schedule": "30m"})
    assert spec["command"] == "sync-backup"
    assert spec["schedule"] == "30m"


def test_normalize_missing_command_raises() -> None:
    with pytest.raises(cron.CronJobSpecError):
        cron.normalize_cronjob_spec({"schedule": "@hourly"})


def test_normalize_non_str_command_raises() -> None:
    with pytest.raises(cron.CronJobSpecError):
        cron.normalize_cronjob_spec({"command": 7, "schedule": "@hourly"})


@pytest.mark.parametrize(
    "schedule",
    [
        "@monthly",  # not in the named set
        "0m",  # leading zero not allowed by the interval regex
        "5s",  # unit not in {m, h}
        "m",  # missing count
        "5",  # missing unit
        7,  # not a string at all
    ],
)
def test_normalize_bad_schedule_forms_raise(schedule) -> None:
    with pytest.raises(cron.CronJobSpecError):
        cron.normalize_cronjob_spec({"command": "echo hi", "schedule": schedule})


def test_normalize_non_dict_node_selector_raises() -> None:
    with pytest.raises(cron.CronJobSpecError):
        cron.normalize_cronjob_spec(
            {"command": "echo hi", "schedule": "@hourly", "nodeSelector": ["gpu"]}
        )


@pytest.mark.parametrize(
    "schedule,expected",
    [
        ("@hourly", 3600),
        ("@daily", 86400),
        ("@weekly", 604800),
        ("15m", 900),
        ("2h", 7200),
    ],
)
def test_schedule_period_seconds(schedule, expected) -> None:
    assert cron.schedule_period_seconds(schedule) == expected


def test_next_run_adds_one_period() -> None:
    assert cron.next_run("@hourly", "2026-07-27T12:00:00Z") == "2026-07-27T13:00:00Z"
    assert cron.next_run("30m", "2026-07-27T12:00:00Z") == "2026-07-27T12:30:00Z"


def test_is_missed_just_inside_boundary_is_false() -> None:
    # period 3600s + grace 60s = 3660s; exactly on the boundary is not missed.
    assert cron.is_missed("@hourly", "2026-07-27T12:00:00Z", "2026-07-27T13:01:00Z") is False


def test_is_missed_just_outside_boundary_is_true() -> None:
    assert cron.is_missed("@hourly", "2026-07-27T12:00:00Z", "2026-07-27T13:01:01Z") is True


def test_is_missed_respects_custom_grace() -> None:
    before = cron.is_missed("15m", "2026-07-27T12:00:00Z", "2026-07-27T12:15:05Z", grace_s=10)
    after = cron.is_missed("15m", "2026-07-27T12:00:00Z", "2026-07-27T12:15:11Z", grace_s=10)
    assert before is False
    assert after is True


def test_explain_registers_cronjob_kind() -> None:
    assert "cronjob" in explain()["kinds"]
    cronjob = explain("cronjob")
    assert cronjob["kind"] == "CronJob"
    assert "MissedRun" in cronjob["conditions"]
    assert any("cronjob" in a for a in cronjob["actions"])
