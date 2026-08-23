"""Tests for the fleet-wide freeze kill-switch."""

from __future__ import annotations

import pytest

from skcapstone.fleet import store


def test_unfrozen_by_default(paths) -> None:
    assert store.is_frozen(paths) is False
    assert store.actuation_allowed(paths) is True


def test_freeze_round_trip(paths, operator) -> None:
    payload = store.set_frozen(paths, True, writer=operator, reason="incident drill")
    assert payload["frozen"] is True
    assert store.is_frozen(paths) is True
    assert store.actuation_allowed(paths) is False
    assert paths.freeze_path().exists()
    store.set_frozen(paths, False, writer=operator)
    assert store.is_frozen(paths) is False


def test_only_operator_may_toggle(paths, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        store.set_frozen(paths, True, writer=noded41)


def test_garbage_freeze_file_fails_safe_frozen(paths) -> None:
    paths.freeze_path().parent.mkdir(parents=True, exist_ok=True)
    paths.freeze_path().write_text("not json")
    assert store.is_frozen(paths) is True  # unreadable flag = halt, not run
