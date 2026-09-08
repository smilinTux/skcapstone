from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.seat_cycle_overlap import (
    RecurringCycleSchedule,
    seat_cycle_runs_overlap,
)

_REFERENCE = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_next_link_and_mero_runs_overlap() -> None:
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=20))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert seat_cycle_runs_overlap("link", link, "mero", mero, after=_REFERENCE)


def test_next_runs_do_not_overlap_when_separated() -> None:
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=5))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert not seat_cycle_runs_overlap("link", link, "mero", mero, after=_REFERENCE)


def test_end_equal_to_other_start_is_not_overlap() -> None:
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=10))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert not seat_cycle_runs_overlap("link", link, "mero", mero, after=_REFERENCE)


def test_overlap_is_symmetric_across_seat_order() -> None:
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=20))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert seat_cycle_runs_overlap("mero", mero, "link", link, after=_REFERENCE)


def test_next_run_is_strictly_after_reference_time() -> None:
    at_link_boundary = _REFERENCE.replace(minute=5)
    link = RecurringCycleSchedule("5 * * * *", timedelta(minutes=20))
    mero = RecurringCycleSchedule("15 * * * *", timedelta(minutes=10))

    assert not seat_cycle_runs_overlap("link", link, "mero", mero, after=at_link_boundary)


@pytest.mark.parametrize(
    ("first_seat", "second_seat"),
    [("link", "link"), ("mero", "mero"), ("jarvis", "mero")],
)
def test_requires_one_link_and_one_mero(first_seat: str, second_seat: str) -> None:
    schedule = RecurringCycleSchedule("0 * * * *", timedelta(minutes=1))

    with pytest.raises(ValueError):
        seat_cycle_runs_overlap(  # type: ignore[arg-type]
            first_seat,
            schedule,
            second_seat,
            schedule,
            after=_REFERENCE,
        )


def test_requires_timezone_aware_reference_time() -> None:
    schedule = RecurringCycleSchedule("0 * * * *", timedelta(minutes=1))

    with pytest.raises(ValueError, match="timezone-aware"):
        seat_cycle_runs_overlap(
            "link",
            schedule,
            "mero",
            schedule,
            after=datetime(2026, 9, 5, 12, 0),
        )


def test_rejects_non_positive_duration() -> None:
    with pytest.raises(ValueError, match="positive timedelta"):
        RecurringCycleSchedule("0 * * * *", timedelta(0))


def test_rejects_invalid_cron_expression() -> None:
    invalid = RecurringCycleSchedule("not a cron", timedelta(minutes=1))
    valid = RecurringCycleSchedule("0 * * * *", timedelta(minutes=1))

    with pytest.raises(ValueError, match="invalid cron schedule"):
        seat_cycle_runs_overlap("link", invalid, "mero", valid, after=_REFERENCE)
