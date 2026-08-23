"""Fail-closed controls for Atlas physical execution."""

from __future__ import annotations

import json

import pytest

from skcapstone.operator_seat import safety


def test_action_fingerprint_is_stable_and_scope_bound():
    first = {"app": "skchat", "condition": "DaemonReady", "object": "d", "action": "restart"}
    reordered = {"action": "restart", "object": "d", "condition": "DaemonReady", "app": "skchat"}
    assert safety.action_fingerprint(first) == safety.action_fingerprint(reordered)
    assert safety.action_fingerprint(first) != safety.action_fingerprint({**first, "app": "other"})


def test_cooldown_retry_budget_and_circuit_persist(tmp_path):
    state = safety.ExecutionState(tmp_path, cooldown_seconds=10, retry_budget=2)
    key = "a" * 64
    assert state.eligibility(key, 100) == (True, None)
    state.record(key, 100, success=False, reason="still firing")
    assert state.eligibility(key, 105) == (False, "cooldown")
    assert state.eligibility(key, 111) == (True, None)
    state.record(key, 111, success=False, reason="still firing")
    assert state.eligibility(key, 1000) == (False, "circuit-open")
    persisted = json.loads((tmp_path / "execution-state.json").read_text())
    assert persisted["actions"][key]["consecutive_failures"] == 2


def test_success_resets_failure_budget(tmp_path):
    state = safety.ExecutionState(tmp_path, cooldown_seconds=0, retry_budget=2)
    state.record("k", 1, success=False)
    state.record("k", 2, success=True)
    payload = json.loads((tmp_path / "execution-state.json").read_text())
    assert payload["actions"]["k"]["consecutive_failures"] == 0
    assert payload["actions"]["k"]["circuit_open"] is False


def test_corrupt_state_fails_closed(tmp_path):
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "execution-state.json").write_text("not json")
    with pytest.raises(RuntimeError, match="unreadable"):
        safety.ExecutionState(tmp_path).eligibility("k", 1)


def test_single_flight_refuses_overlap(tmp_path):
    first = safety.ExecutionState(tmp_path)
    second = safety.ExecutionState(tmp_path)
    with first.single_flight():
        with pytest.raises(RuntimeError, match="another Atlas"):
            with second.single_flight():
                pass
