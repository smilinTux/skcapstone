from datetime import datetime, timezone

import pytest

from skcapstone.niobe_activation import ActivationError, parse_activation

NOW = datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)


def record(**overrides):
    value = {
        "schema": "skfleet.niobe-activation/v1",
        "state": "active",
        "seat": "niobe",
        "decision_id": "casey-niobe-20260906",
        "authorized_by": "casey",
        "card_id": "c4e7a9b2",
        "card_revision": "a" * 64,
        "host": "chiap08",
        "live_unit": "skfleet-niobe-live.timer",
        "product_scope": ["skcapstone", "skdashboard", "skworld"],
        "card_label": "seat-niobe",
        "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
        "denied_actions": ["merge", "deploy", "application_actuation", "external_dispatch"],
        "expires_at": "2026-09-07T20:00:00+00:00",
        "rollback": {
            "owner": "casey",
            "action": "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer",
        },
    }
    value.update(overrides)
    return value


def test_exact_casey_scope_is_accepted():
    activation = parse_activation(record(), now=NOW)
    assert activation.allowed_actions == {"claim", "release", "launch", "stop", "reassign"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "chiap01"),
        ("authorized_by", "jarvis"),
        ("state", "pending"),
        ("denied_actions", ["merge"]),
    ],
)
def test_scope_or_authority_mismatch_is_rejected(field, value):
    with pytest.raises(ActivationError):
        parse_activation(record(**{field: value}), now=NOW)


def test_expired_activation_is_rejected():
    with pytest.raises(ActivationError, match="expired"):
        parse_activation(record(expires_at="2026-09-06T20:00:00+00:00"), now=NOW)


def test_extra_live_action_is_rejected():
    with pytest.raises(ActivationError, match="bounded"):
        parse_activation(
            record(allowed_actions=["claim", "release", "launch", "stop", "reassign", "rotate"]),
            now=NOW,
        )


def test_missing_rollback_is_rejected():
    with pytest.raises(ActivationError, match="rollback"):
        parse_activation(record(rollback=None), now=NOW)


@pytest.mark.parametrize(
    "field,value",
    [
        ("card_id", "wrong"),
        ("card_revision", "short"),
        ("live_unit", "skfleet-rotate.timer"),
        ("card_revision", "z" * 64),
    ],
)
def test_activation_card_and_unit_fence_is_exact(field, value):
    with pytest.raises(ActivationError):
        parse_activation(record(**{field: value}), now=NOW)
