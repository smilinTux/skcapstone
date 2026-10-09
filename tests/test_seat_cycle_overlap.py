from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skcapstone.seat_cycle_overlap import (
    RecurringCycleSchedule,
    seat_cycle_runs_overlap,
)


def test_next_link_and_mero_runs_overlap() -> None:
    after = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=20))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert seat_cycle_runs_overlap("link", link, "mero", mero, after=after)


def test_boundary_touching_runs_do_not_overlap() -> None:
    after = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=10))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert not seat_cycle_runs_overlap("link", link, "mero", mero, after=after)
