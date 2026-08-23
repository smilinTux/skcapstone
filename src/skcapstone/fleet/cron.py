"""Fleet object model for scheduled work (Phase 4 CronController, step 1).

Pure spec normalization and time math for the CronJob kind, mirroring the
service kind's conventions. No clock reads happen here: every time helper
takes its ISO8601 UTC timestamps as arguments. skscheduler wiring for
CronJob is a later operational card.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_SCHEDULE_NAMED = {"@hourly", "@daily", "@weekly"}
_SCHEDULE_INTERVAL = re.compile(r"^[1-9][0-9]*[mh]$")

_NAMED_PERIOD_SECONDS = {
    "@hourly": 3600,
    "@daily": 86400,
    "@weekly": 604800,
}

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class CronJobSpecError(Exception):
    """A CronJob spec failed validation."""


def _valid_schedule(schedule: str) -> bool:
    return schedule in _SCHEDULE_NAMED or bool(_SCHEDULE_INTERVAL.match(schedule))


def normalize_cronjob_spec(spec: dict) -> dict:
    """Validate a CronJob spec and apply defaults.

    Args:
        spec: Raw spec dict with at least 'command' and 'schedule'.

    Returns:
        The normalized spec: command, schedule, enabled, nodeSelector, deleted.

    Raises:
        CronJobSpecError: 'command' is missing or not a string, 'schedule'
            is not one of @hourly/@daily/@weekly and does not match the
            interval form (e.g. 30m, 2h), or 'nodeSelector' is present and
            not a dict.
    """
    command = spec.get("command")
    if not isinstance(command, str):
        raise CronJobSpecError(f"command must be a string: {command!r}")
    schedule = spec.get("schedule")
    if not isinstance(schedule, str) or not _valid_schedule(schedule):
        raise CronJobSpecError(f"invalid schedule: {schedule!r}")
    node_selector = spec.get("nodeSelector", {})
    if not isinstance(node_selector, dict):
        raise CronJobSpecError(f"nodeSelector must be a dict: {node_selector!r}")
    return {
        "command": command,
        "schedule": schedule,
        "enabled": spec.get("enabled", True),
        "nodeSelector": node_selector,
        "deleted": spec.get("deleted", False),
    }


def schedule_period_seconds(schedule: str) -> int:
    """The number of seconds in one period of a schedule.

    Raises:
        CronJobSpecError: schedule is not a recognized named or interval form.
    """
    if schedule in _NAMED_PERIOD_SECONDS:
        return _NAMED_PERIOD_SECONDS[schedule]
    if _SCHEDULE_INTERVAL.match(schedule):
        count, unit = int(schedule[:-1]), schedule[-1]
        return count * 60 if unit == "m" else count * 3600
    raise CronJobSpecError(f"invalid schedule: {schedule!r}")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, _ISO_FORMAT).replace(tzinfo=timezone.utc)


def _format_iso(value: datetime) -> str:
    return value.strftime(_ISO_FORMAT)


def next_run(schedule: str, after_iso: str) -> str:
    """The next run time: after_iso plus one schedule period."""
    period = schedule_period_seconds(schedule)
    return _format_iso(_parse_iso(after_iso) + timedelta(seconds=period))


def is_missed(schedule: str, baseline_iso: str, now_iso: str, grace_s: int = 60) -> bool:
    """True when now is more than one schedule period plus grace past baseline."""
    period = schedule_period_seconds(schedule)
    elapsed = (_parse_iso(now_iso) - _parse_iso(baseline_iso)).total_seconds()
    return elapsed > period + grace_s
