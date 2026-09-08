"""Pure overlap detection for recurring Link and Mero seat cycles.

Cycle windows are half-open intervals: ``[next_run, next_run + duration)``.
Consequently, one cycle ending exactly when the other starts is not an overlap.
The module performs no locking, persistence, deferral, or lifecycle enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

SeatIdentity = Literal["link", "mero"]
_ALLOWED_SEATS = frozenset({"link", "mero"})


@dataclass(frozen=True)
class RecurringCycleSchedule:
    """A cron recurrence and the maximum time occupied by each run."""

    cron: str
    duration: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.cron, str) or not self.cron.strip():
            raise ValueError("cron must be a non-empty string")
        if not isinstance(self.duration, timedelta) or self.duration <= timedelta(0):
            raise ValueError("duration must be a positive timedelta")

    def next_run(self, after: datetime) -> datetime:
        """Return the first scheduled run strictly after ``after``."""

        if not isinstance(after, datetime) or after.tzinfo is None:
            raise ValueError("after must be a timezone-aware datetime")
        try:
            return croniter(self.cron, after, ret_type=datetime).get_next(datetime)
        except (CroniterBadCronError, CroniterBadDateError, KeyError) as exc:
            raise ValueError(f"invalid cron schedule: {self.cron!r}") from exc


def seat_cycle_runs_overlap(
    first_seat: SeatIdentity,
    first_schedule: RecurringCycleSchedule,
    second_seat: SeatIdentity,
    second_schedule: RecurringCycleSchedule,
    *,
    after: datetime,
) -> bool:
    """Return whether the seats' next recurring run windows intersect.

    Exactly one Link schedule and one Mero schedule must be supplied. The seat
    order is immaterial. Only the first run strictly after ``after`` is tested.
    Boundary-touching windows do not overlap.
    """

    seats = (first_seat, second_seat)
    if any(seat not in _ALLOWED_SEATS for seat in seats):
        raise ValueError("seat identities must be 'link' or 'mero'")
    if first_seat == second_seat:
        raise ValueError("overlap detection requires one link and one mero seat")
    if not isinstance(first_schedule, RecurringCycleSchedule) or not isinstance(
        second_schedule, RecurringCycleSchedule
    ):
        raise TypeError("schedules must be RecurringCycleSchedule instances")

    first_start = first_schedule.next_run(after)
    second_start = second_schedule.next_run(after)
    first_end = first_start + first_schedule.duration
    second_end = second_start + second_schedule.duration

    return first_start < second_end and second_start < first_end
