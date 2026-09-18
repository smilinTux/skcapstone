import pytest
from skcapstone.fleet.claim_expiry import (
    ClaimObservation,
    evaluate,
    ttl_seconds_from_env,
    mode_from_env,
)

HOUR = 3600.0
NOW = 1_000_000.0


def obs(cid="c1", owner="jarvis", rev="r1", last=None):
    return ClaimObservation(
        card_id=cid,
        owner=owner,
        claim_revision=rev,
        last_owner_event_at=NOW - 50 * HOUR if last is None else last,
    )


def test_idle_beyond_ttl_is_reclaimable():
    v = evaluate([obs()], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is True
    assert v.idle_seconds == pytest.approx(50 * HOUR)


def test_idle_within_ttl_is_not_reclaimable():
    v = evaluate([obs(last=NOW - 10 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "within-ttl"


def test_exactly_at_ttl_is_not_reclaimable():
    """The boundary is exclusive, so a claim is never reclaimed a moment early."""
    v = evaluate([obs(last=NOW - 48 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False


def test_bare_session_owner_is_reclaimable_not_skipped():
    """The case the current reaper cannot reach at all: an owner that does not
    look like a fleet worker. 146 of the 349 held claims look like this."""
    for name in ("jarvis", "codex", "seraph", "lumina", "tank"):
        v = evaluate([obs(owner=name)], now=NOW, ttl_seconds=48 * HOUR)[0]
        assert v.reclaimable is True, name


def test_missing_claim_revision_is_never_reclaimable():
    """Without a revision there is no CAS fence, so a release could race a
    re-claim. Refuse rather than risk it."""
    v = evaluate([obs(rev=None)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "no-claim-revision"


def test_unknown_last_event_is_never_reclaimable():
    v = evaluate([obs(last=0.0)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "no-owner-activity"


def test_future_timestamp_is_never_reclaimable():
    """Clock skew across hosts must not manufacture a reclaim."""
    v = evaluate([obs(last=NOW + 5 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "future-timestamp"


def test_ttl_from_env_default_is_48h():
    assert ttl_seconds_from_env({}) == 48 * HOUR


def test_ttl_from_env_override():
    assert ttl_seconds_from_env({"SKFLEET_CLAIM_TTL_H": "6"}) == 6 * HOUR


def test_ttl_from_env_rejects_garbage_and_nonpositive():
    for bad in ("0", "-1", "abc", ""):
        assert ttl_seconds_from_env({"SKFLEET_CLAIM_TTL_H": bad}) == 48 * HOUR


def test_mode_defaults_off_and_unknown_is_off():
    assert mode_from_env({}) == "off"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": "banana"}) == "off"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": "ENFORCE"}) == "enforce"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": " report "}) == "report"
