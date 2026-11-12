"""Evidence freshness: structured timestamp, a maximum age, and fail closed."""

from __future__ import annotations

import pytest

from skcapstone.operator_seat import evidence

NOW = "2026-08-20T00:00:00Z"


def _prop(**stamp):
    return {"action": "restart", "evidence": {"source": "obs:skchat:bridge", **stamp}}


def test_fresh_evidence_is_accepted():
    assert evidence.refusal(_prop(observed_at="2026-08-19T23:59:30Z"), NOW, 600) is None


def test_exactly_the_max_age_is_accepted():
    assert evidence.refusal(_prop(observed_at="2026-08-19T23:50:00Z"), NOW, 600) is None


def test_older_than_max_age_is_refused():
    reason = evidence.refusal(_prop(observed_at="2026-08-19T23:49:59Z"), NOW, 600)
    assert reason is not None
    assert "stale" in reason
    assert "600" in reason


def test_seven_year_old_fabricated_evidence_is_refused():
    reason = evidence.refusal(_prop(observed_at="2019-01-01T00:00:00Z"), NOW, 600)
    assert reason is not None and "stale" in reason


@pytest.mark.parametrize(
    "proposal",
    [
        {"action": "restart"},
        {"action": "restart", "evidence": None},
        {"action": "restart", "evidence": {}},
        {"action": "restart", "evidence": {"source": "obs:x"}},
        {"action": "restart", "evidence": {"observed_at": "2026-08-19T23:59:30Z"}},
        {"action": "restart", "evidence": "2026-08-19T23:59:30Z"},
        # The legacy free-text field alone is never freshness evidence.
        {"action": "restart", "evidence_ref": "obs:2026-08-19T23:59:30Z:bridge"},
    ],
)
def test_missing_or_incomplete_evidence_fails_closed(proposal):
    reason = evidence.refusal(proposal, NOW, 600)
    assert reason is not None
    assert "missing" in reason


@pytest.mark.parametrize(
    "observed_at",
    ["", "yesterday", "2026-13-45T00:00:00Z", "2026-08-19T23:59:30", 1787270370, True, None, []],
)
def test_unparseable_timestamp_fails_closed(observed_at):
    reason = evidence.refusal(_prop(observed_at=observed_at), NOW, 600)
    assert reason is not None
    assert "missing" in reason or "unparseable" in reason


def test_naive_timestamp_is_unparseable_not_assumed_utc():
    reason = evidence.refusal(_prop(observed_at="2026-08-19T23:59:30"), NOW, 600)
    assert reason is not None and "unparseable" in reason


def test_future_timestamp_beyond_skew_is_refused():
    reason = evidence.refusal(_prop(observed_at="2026-08-20T00:05:00Z"), NOW, 600)
    assert reason is not None and "future" in reason


def test_small_forward_clock_skew_is_tolerated():
    assert evidence.refusal(_prop(observed_at="2026-08-20T00:00:30Z"), NOW, 600) is None


def test_offset_timestamps_are_compared_in_utc():
    # 18:59:30-05:00 is 23:59:30Z.
    assert evidence.refusal(_prop(observed_at="2026-08-19T18:59:30-05:00"), NOW, 600) is None


@pytest.mark.parametrize("max_age", [0, -1, float("nan"), float("inf"), None, True, "600"])
def test_a_bad_max_age_fails_closed(max_age):
    reason = evidence.refusal(_prop(observed_at="2026-08-19T23:59:30Z"), NOW, max_age)
    assert reason is not None and "max age" in reason


def test_an_unparseable_pass_clock_fails_closed():
    reason = evidence.refusal(_prop(observed_at="2026-08-19T23:59:30Z"), "not-a-time", 600)
    assert reason is not None


def test_ledger_ref_carries_source_and_timestamp():
    prop = _prop(observed_at="2026-08-19T23:59:30Z")
    assert evidence.ledger_ref(prop) == "obs:skchat:bridge@2026-08-19T23:59:30Z"


def test_ledger_ref_records_the_stamp_first_and_keeps_an_explicit_ref_too():
    prop = {**_prop(observed_at="2026-08-19T23:59:30Z"), "evidence_ref": "card:abc"}
    assert evidence.ledger_ref(prop) == "obs:skchat:bridge@2026-08-19T23:59:30Z; card:abc"


def test_ledger_ref_without_a_stamp_is_just_the_explicit_ref():
    assert evidence.ledger_ref({"evidence_ref": "card:abc"}) == "card:abc"


def test_a_long_explicit_ref_never_pushes_the_stamp_out_of_the_ledger_field():
    prop = {**_prop(observed_at="2026-08-19T23:59:30Z"), "evidence_ref": "x" * 5000}
    ref = evidence.ledger_ref(prop)
    assert ref is not None and len(ref) <= 1024
    assert ref.startswith("obs:skchat:bridge@2026-08-19T23:59:30Z; ")


def test_ledger_ref_is_none_without_any_evidence():
    assert evidence.ledger_ref({"action": "restart"}) is None


def test_ledger_ref_is_bounded_to_the_ledger_field_limit():
    prop = {"evidence": {"source": "s" * 5000, "observed_at": "2026-08-19T23:59:30Z"}}
    ref = evidence.ledger_ref(prop)
    assert ref is not None and len(ref) <= 1024 and ref.endswith("@2026-08-19T23:59:30Z")


def test_the_max_age_is_capped_so_the_check_cannot_be_neutralised():
    assert evidence.MAX_ALLOWED_AGE_SECONDS == 3600.0
    fresh = _prop(observed_at="2026-08-19T23:59:30Z")
    assert evidence.refusal(fresh, NOW, 3600) is None
    for too_big in (3601, 86400, 1e18, 10**400):
        reason = evidence.refusal(fresh, NOW, too_big)
        assert reason is not None and "max age" in reason, too_big


def test_a_huge_integer_max_age_is_a_refusal_not_a_crash():
    # math.isfinite(10**400) raises OverflowError; the pass must not die on it.
    reason = evidence.refusal(_prop(observed_at="2026-08-19T23:59:30Z"), NOW, 10**400)
    assert reason is not None and "max age" in reason


@pytest.mark.parametrize("observed_at", ["0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"])
def test_a_timestamp_that_overflows_utc_conversion_is_unparseable_not_a_crash(observed_at):
    reason = evidence.refusal(_prop(observed_at=observed_at), NOW, 600)
    assert reason is not None
