"""Round 7 polish (sixth independent review of caf83b8b, which PASSED): the loop's sweeps and
escalate-lane drafting honor the actuator-node rule, the node-local directory chain is checked
end to end, an OSError taking a lock is contained per proposal, and the drill captures the real
actuator rule lazily."""

from __future__ import annotations

import os
import stat
from datetime import timedelta

import pytest

from skcapstone.operator_seat import action_ledger, dispatch, loop, node_local, safety

from .test_dispatch_e2e import (  # noqa: F401  (world is a fixture)
    _escalated_intent,
    _loop_pass,
    _manual_intent,
    _normal_change,
    _stamp,
    _standard_bound_change,
    _strand,
    _sweep,
    _vote,
    world,
)

NOT_ACTUATOR = "this node (node-b) is not the designated actuator (node-a)"


# --------------------------------------------------------------------------
# 1: a non-actuator node's loop does not sweep, close or draft
# --------------------------------------------------------------------------


def _actuators_intents(world):
    """Three intents that belong to the actuator's live work."""
    live_change = _normal_change(world)
    _vote(world, live_change.id)
    live = _strand(world, live_change.id, age=timedelta(minutes=30))  # EXECUTING, lock free here
    auto_change = _standard_bound_change(world)
    leftover = _manual_intent(world, auto_change.id, lane="auto", evidence=_stamp(world), tag="-l")
    waiting_change = _normal_change(world)
    _vote(world, waiting_change.id)
    waiting = _manual_intent(world, waiting_change.id, lane="escalate", tag="-w")
    world.time.advance(minutes=20)
    return live, leftover, waiting


def test_a_non_actuator_nodes_loop_leaves_the_actuators_intents_alone(world, monkeypatch):
    live, leftover, waiting = _actuators_intents(world)
    world.heal()  # on THIS node's local view the condition reads cleared
    monkeypatch.setattr(node_local, "actuator_refusal", lambda: NOT_ACTUATOR)
    res = _loop_pass(world, target_known=lambda p: False)
    assert res["stranded"] == [] and res["closed_episodes"] == []
    assert world.ledger.current_state(live) is action_ledger.ActionState.EXECUTING
    assert world.ledger.current_state(leftover) is action_ledger.ActionState.PROPOSED
    assert world.ledger.current_state(waiting) is action_ledger.ActionState.PROPOSED
    assert world.calls == []


def test_the_actuator_nodes_loop_does_all_three_sweeps(world):
    live, leftover, waiting = _actuators_intents(world)
    world.heal()
    res = _loop_pass(world, target_known=lambda p: False)
    assert live in res["stranded"]
    assert world.ledger.current_state(live) is action_ledger.ActionState.ESCALATED
    assert world.ledger.current_state(leftover) is action_ledger.ActionState.ESCALATED
    assert world.ledger.current_state(waiting) is action_ledger.ActionState.ESCALATED
    assert waiting in res["closed_episodes"]


def test_a_non_actuator_node_does_not_draft_escalate_lane_changes_or_intents(world, monkeypatch):
    monkeypatch.setattr(node_local, "actuator_refusal", lambda: NOT_ACTUATOR)
    res = _loop_pass(world, target_known=lambda p: False)
    assert res["outcomes"][0]["outcome"] == "escalated (parked for approval)"
    assert res["outcomes"][0]["intent_id"] is None
    assert world.ledger.list_intents() == []
    assert world.itil.list_changes() == []


def test_the_actuator_node_still_drafts_the_escalate_lane(world):
    intent_id = _escalated_intent(world)
    assert world.ledger.list_intents() == [intent_id]
    assert len(world.itil.list_changes()) == 1


# --------------------------------------------------------------------------
# 2: the whole node-local directory chain is checked
# --------------------------------------------------------------------------


def test_private_chain_refuses_a_symlink_anywhere_in_the_chain(tmp_path):
    base = tmp_path / "state"
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    # a symlinked MIDDLE component
    base.mkdir(mode=0o700)
    os.symlink(real, base / "atlas")
    with pytest.raises(node_local.UnsafeDirectoryError, match="symlink"):
        node_local.private_chain(base, "atlas", "change-locks")
    # a symlinked BASE (the state directory itself)
    linked = tmp_path / "linked-state"
    os.symlink(real, linked)
    with pytest.raises(node_local.UnsafeDirectoryError, match="symlink"):
        node_local.private_chain(linked, "atlas")


def test_private_chain_creates_a_private_chain_and_accepts_it_again(tmp_path):
    leaf = node_local.private_chain(tmp_path / "state", "atlas", "change-locks")
    for part in (tmp_path / "state", tmp_path / "state" / "atlas", leaf):
        assert stat.S_IMODE(part.stat().st_mode) == 0o700
    assert node_local.private_chain(tmp_path / "state", "atlas", "change-locks") == leaf


def test_private_chain_refuses_a_loose_base(tmp_path):
    base = tmp_path / "state"
    base.mkdir()
    base.chmod(0o755)
    with pytest.raises(node_local.UnsafeDirectoryError, match="not private"):
        node_local.private_chain(base, "atlas")


def test_a_symlinked_state_chain_refuses_a_dispatch(world, tmp_path):
    change = _normal_change(world)
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = node_local.state_dir()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.symlink(elsewhere, state / "atlas")
    outcomes = _sweep(world)
    assert world.calls == []
    assert "lock directory" in outcomes[0].outcome


def test_the_single_flight_lock_directory_is_verified_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setenv(node_local.ENV_STATE_DIR, str(tmp_path / "state"))
    state = safety.ExecutionState(
        tmp_path / "exec",
        lock_dir=node_local.state_dir() / "atlas",
        verify_lock_dir=True,
    )
    with state.single_flight():
        pass  # a clean chain works
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (node_local.state_dir() / "atlas").rmdir()
    os.symlink(elsewhere, node_local.state_dir() / "atlas")
    with pytest.raises(node_local.UnsafeDirectoryError, match="symlink"):
        with state.single_flight():
            pass


def test_the_cli_builds_its_execution_state_with_the_verified_lock_directory(tmp_path):
    from skcapstone.fleet.paths import FleetPaths
    from skcapstone.operator_seat import cli

    state = cli._execution_state(FleetPaths(root=tmp_path / "fleet"))
    assert state.lock_path.parent == node_local.state_dir() / "atlas"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    node_local.state_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    os.symlink(elsewhere, node_local.state_dir() / "atlas")
    with pytest.raises(node_local.UnsafeDirectoryError):
        with state.single_flight():
            pass


# --------------------------------------------------------------------------
# 3: an OSError taking a lock is contained per proposal
# --------------------------------------------------------------------------


def test_an_oserror_taking_a_lock_is_contained_per_proposal(world, monkeypatch):
    from skcapstone.operator_seat import act_dispatch

    real_dispatch = dispatch.dispatch_intent
    seen = []

    def flaky(*args, **kwargs):
        seen.append(args[3])
        if len(seen) == 1:
            raise OSError(28, "No space left on device")
        return real_dispatch(*args, **kwargs)

    monkeypatch.setattr(dispatch, "dispatch_intent", flaky)
    proposals = [
        {
            "app": "skchat",
            "condition": "BridgeAlive",
            "action": "restart-telegram-bridge",
            "object": obj,
            "evidence": _stamp(world),
        }
        for obj in ("telegram-bridge", "telegram-bridge-2")
    ]
    res = loop.run_once(
        world.paths,
        now_iso=__import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        propose=lambda b, r: proposals,
        explain=act_dispatch.merged_explain(),
        decisions_dir=str(world.decisions),
        apply_fn=lambda p, c: {"performed": True},
        execute=True,
        lifecycle_ledger=world.ledger,
        itil=world.itil,
        clock=lambda: world.time.now,
        emit=lambda _s: None,
    )
    first, second = res["outcomes"]
    assert first["outcome"].startswith("error: ") and "No space left" in first["outcome"]
    assert len(seen) == 2, "the pass carried on to the next proposal"
    assert not second["outcome"].startswith("error: ")
    assert world.ledger.current_state(first["intent_id"]) is action_ledger.ActionState.ESCALATED


def test_the_deadline_still_aborts_the_pass_and_voids_the_intent(world, monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("Atlas operator pass exceeded its maximum runtime")

    monkeypatch.setattr(dispatch, "dispatch_intent", boom)
    with pytest.raises(TimeoutError):
        _loop_pass(world)
    assert action_ledger.ActionState.PROPOSED not in [
        world.ledger.current_state(i) for i in world.ledger.list_intents()
    ]


def test_dispatch_intent_returns_an_error_when_the_lock_cannot_be_taken(world, monkeypatch):
    from skcapstone.operator_seat import skchat_adapter

    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")

    def broken(change_id, *, blocking=False):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(dispatch, "_change_lock", broken)
    outcome = dispatch.dispatch_intent(
        world.paths,
        world.ledger,
        world.itil,
        intent_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=lambda p, c: {"performed": True},
        now_iso="2026-01-01T00:00:00Z",
        clock=lambda: world.time.now,
    )
    assert outcome.outcome.startswith("error: could not take the change lock")
    assert world.calls == []


# --------------------------------------------------------------------------
# 4: the drill captures the real rule lazily; docstrings tell the truth
# --------------------------------------------------------------------------


def test_the_real_actuator_rule_is_reachable_even_when_the_gate_is_patched_open():
    assert node_local.actuator_refusal() is None  # the conftest opened the gate
    assert node_local.real_actuator_refusal is not node_local.actuator_refusal
    # With nothing designated on this test's environment, the REAL rule refuses.
    assert node_local.real_actuator_refusal() is not None or node_local.designated_actuator()


def test_the_drill_does_not_capture_the_actuator_rule_at_import():
    from skcapstone.operator_seat import fault_injection_drill as drill

    assert not hasattr(drill, "_REAL_ACTUATOR_REFUSAL")


def test_the_approval_docstrings_match_the_behaviour():
    assert "close nothing" not in dispatch.approval_refusal.__doc__.split('``"future"``')[1][:120]
    assert "skips the age limit" not in dispatch.dispatch_intent.__doc__
