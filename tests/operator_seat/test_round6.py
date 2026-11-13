"""Round 6 (fifth independent review of 549d4069): live dispatches are never stranded,
future-dated votes do not buy time, episodes close in the loop, locks and actuation are
node-local, leftovers are swept.

End to end through ITIL, the ledger and ``dispatch_intent`` with the real
``act_dispatch.build_apply_fn`` and the real skchat adapter (the ``world`` fixture).
"""

from __future__ import annotations

import os
import stat
from datetime import timedelta

import pytest

from skcapstone.fleet import store
from skcapstone.operator_seat import action_ledger, dispatch, loop, safety, skchat_adapter

from .test_dispatch_e2e import (  # noqa: F401  (world is a fixture)
    BIND,
    WEEK,
    _apply_fn,
    _dispatch_one,
    _escalated_intent,
    _iso,
    _loop_pass,
    _manual_intent,
    _normal_change,
    _stamp,
    _standard_bound_change,
    _states,
    _sweep,
    _vote,
    world,
)

HUMAN = store.Writer(role="operator", node="node-158", identity="chef")


# --------------------------------------------------------------------------
# 1: a live dispatch is never mistaken for a stranded one
# --------------------------------------------------------------------------


def _long_dispatch(world, *, advance, sweeper_ahead=timedelta(0), pass_started_ago=timedelta(0)):
    """Dispatch one human-approved intent whose actuation takes ``advance`` of wall time, with a
    sweeper running on another 'process' (its clock ``sweeper_ahead``) while it is in flight."""
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    alerts, rollbacks, swept = [], [], []
    started = world.time.now

    def apply_fn(prop, classification):
        world.time.advance(**advance)
        swept.append(
            dispatch.escalate_stranded(
                world.ledger,
                world.itil,
                now_iso=_iso(world.time.now),
                clock=lambda: world.time.now + sweeper_ahead,
                emit=alerts.append,
            )
        )
        return _apply_fn(world)(prop, classification)

    outcome = dispatch.dispatch_intent(
        world.paths,
        world.ledger,
        world.itil,
        intent_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
        now_iso=_iso(started - pass_started_ago),
        clock=lambda: world.time.now,
        emit=alerts.append,
    )
    return intent_id, outcome, swept, alerts, rollbacks


def test_an_eleven_minute_actuation_is_not_escalated_as_stranded_mid_flight(world):
    intent_id, outcome, swept, alerts, rollbacks = _long_dispatch(world, advance={"minutes": 11})
    assert swept == [[]], "the live dispatch holds its change; the sweep must skip it"
    assert outcome.outcome == "verified"
    assert rollbacks == [], "a successful fix is never rolled back"
    assert not [a for a in alerts if "ALERT" in a]
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def test_events_are_stamped_from_the_clock_not_the_pass_start(world):
    """The second intent of a long pass must not look 15 minutes old at 0 seconds."""
    intent_id, outcome, swept, alerts, rollbacks = _long_dispatch(
        world, advance={"seconds": 1}, pass_started_ago=timedelta(minutes=15)
    )
    assert outcome.outcome == "verified"
    assert swept == [[]]
    events = world.ledger.events(intent_id)
    authorized = next(e for e in events if e.state.value == "authorized")
    assert abs((authorized.occurred_at - world.time.now).total_seconds()) < 60


def test_a_sweeper_whose_clock_is_ahead_does_not_escalate_a_dispatch_that_just_started(world):
    intent_id, outcome, swept, alerts, rollbacks = _long_dispatch(
        world, advance={"seconds": 1}, sweeper_ahead=timedelta(minutes=11)
    )
    assert swept == [[]]
    assert outcome.outcome == "verified"
    assert rollbacks == []


def test_a_dispatch_pass_is_single_flight_with_the_loop(world, tmp_path):
    state = safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)
    with state.single_flight():
        with pytest.raises(RuntimeError, match="another Atlas operator pass is active"):
            _sweep(world, execution_state=state)
    assert _sweep(world, execution_state=state) == []  # free again


class _VerifiedAppendFails(action_ledger.ActionLedger):
    def append(self, intent_id, state, **kwargs):
        if state is action_ledger.ActionState.VERIFIED:
            raise ValueError("invalid action transition: simulated")
        return super().append(intent_id, state, **kwargs)


def test_a_failed_verified_append_never_rolls_back_a_fix_that_worked(world, tmp_path):
    change = _normal_change(world)
    _vote(world, change.id)
    world.ledger = _VerifiedAppendFails(tmp_path / "ledger")
    intent_id = _manual_intent(
        world, change.id, lane="escalate", rollback={"action": "restart-daemon"}
    )
    rollbacks, alerts = [], []
    outcome = dispatch.dispatch_intent(
        world.paths,
        world.ledger,
        world.itil,
        intent_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=_apply_fn(world),
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
        now_iso=_iso(world.time.now),
        clock=lambda: world.time.now,
        emit=alerts.append,
    )
    assert rollbacks == []
    assert outcome.outcome.startswith("error:")
    assert "could not be recorded" in outcome.outcome
    assert any("ALERT" in a for a in alerts)
    assert len(world.calls) == 1


def test_the_loops_inline_lane_never_rolls_back_a_fix_that_worked_either(world, tmp_path):
    ledger = _VerifiedAppendFails(tmp_path / "ledger2")
    rollbacks, alerts = [], []
    res = loop.run_once(
        world.paths,
        now_iso=_iso(world.time.now),
        propose=lambda b, r: [
            {
                "app": "skchat",
                "condition": "BridgeAlive",
                "action": "restart-telegram-bridge",
                "object": "telegram-bridge",
                "evidence": _stamp(world),
                "rollback": {"action": "restart-daemon"},
            }
        ],
        explain=__import__(
            "skcapstone.operator_seat.act_dispatch", fromlist=["x"]
        ).merged_explain(),
        decisions_dir=str(world.decisions),
        apply_fn=_apply_fn(world),
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
        execute=True,
        lifecycle_ledger=ledger,
        clock=lambda: world.time.now,
        emit=alerts.append,
    )
    assert rollbacks == []
    assert res["outcomes"][0]["outcome"].startswith("error:")
    assert len(world.calls) == 1


# --------------------------------------------------------------------------
# 2: a future-dated vote cannot buy time
# --------------------------------------------------------------------------


def test_a_future_dated_vote_closes_the_intent_and_retires_the_change(world, monkeypatch):
    from tests import cab_grants

    change = _normal_change(world)
    # Card bada5c86: the grant itself is issued in the future (its signed time counts).
    monkeypatch.setattr(cab_grants, "ISSUED_AT", world.time.now + timedelta(days=5))
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world, max_approval_age_s=WEEK)
    assert world.calls == []
    assert "dated in the future" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    retired = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert retired.status.value in ("rejected", "closed")
    # The vote's own future date arrives: nothing is left to act on.
    world.time.advance(days=5, hours=23)
    assert _sweep(world, max_approval_age_s=WEEK) == []
    assert world.calls == []


# --------------------------------------------------------------------------
# 3: the loop closes an episode it sees clear
# --------------------------------------------------------------------------


def test_the_loop_closes_a_cleared_escalation_so_a_later_vote_cannot_ride_the_next_episode(world):
    first = _escalated_intent(world)
    old_change = world.ledger.read_intent(first).itil_change_id
    world.heal()  # episode 1 ends
    _loop_pass(world, target_known=lambda p: False)
    assert world.ledger.current_state(first) is action_ledger.ActionState.ESCALATED
    retired = next(c for c in world.itil.list_changes() if c.id == old_change)
    assert retired.status.value in ("rejected", "closed")
    # Episode 2, 18 hours later.
    world.time.advance(hours=18)
    world.wedge()
    again = _loop_pass(world, target_known=lambda p: False)
    second = again["outcomes"][0]["intent_id"]
    assert second != first, "a new episode is a new intent"
    assert world.ledger.read_intent(second).itil_change_id != old_change
    # A (late) human vote on the episode-1 change is useless.
    _vote(world, old_change)
    outcomes = _sweep(world)
    assert world.calls == []
    assert all(o.intent_id != first for o in outcomes)


def test_an_escalation_whose_condition_is_still_firing_is_kept_and_reused(world):
    first = _escalated_intent(world)
    again = _loop_pass(world, target_known=lambda p: False)
    assert again["outcomes"][0]["intent_id"] == first
    assert world.ledger.current_state(first) is action_ledger.ActionState.PROPOSED


# --------------------------------------------------------------------------
# 4: node-local locks, a single actuator node
# --------------------------------------------------------------------------


def test_change_locks_live_in_the_node_local_state_dir_not_the_synced_ledger_tree(world):
    from skcapstone.operator_seat import node_local

    change = _normal_change(world)
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    assert _sweep(world)[0].outcome == "verified"
    assert not (world.ledger.root / "change-locks").exists()
    locks = node_local.state_dir() / "atlas" / "change-locks"
    assert (locks / f"{change.id}.lock").is_file()
    assert stat.S_IMODE(locks.stat().st_mode) == 0o700
    assert stat.S_IMODE((locks / f"{change.id}.lock").stat().st_mode) == 0o600


def test_a_lock_directory_that_is_not_private_is_refused(world):
    from skcapstone.operator_seat import node_local

    change = _normal_change(world)
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    locks = node_local.state_dir() / "atlas" / "change-locks"
    locks.mkdir(parents=True, mode=0o700)
    locks.chmod(0o777)
    outcomes = _sweep(world)
    assert world.calls == []
    assert "lock directory" in outcomes[0].outcome


def test_a_symlinked_lock_directory_is_refused(world, tmp_path):
    from skcapstone.operator_seat import node_local

    change = _normal_change(world)
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    locks = node_local.state_dir() / "atlas" / "change-locks"
    locks.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.symlink(elsewhere, locks)
    outcomes = _sweep(world)
    assert world.calls == []
    assert "lock directory" in outcomes[0].outcome


def test_no_designated_actuator_means_nothing_actuates():
    from skcapstone.operator_seat import node_local

    assert node_local.check_actuator(designated=None, this_node="node-a") is not None
    assert "no actuator node" in node_local.check_actuator(designated=None, this_node="n")
    assert node_local.check_actuator(designated="node-b", this_node="node-a") is not None
    assert node_local.check_actuator(designated="node-a", this_node="node-a") is None


def test_the_designation_comes_from_node_local_config_and_defaults_to_none(tmp_path, monkeypatch):
    from skcapstone.operator_seat import node_local

    monkeypatch.delenv("SKOPERATOR_ACTUATOR_NODE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert node_local.designated_actuator() is None
    cfg = tmp_path / "cfg" / "skcapstone"
    cfg.mkdir(parents=True)
    (cfg / "atlas-actuator-node").write_text("node-a\n")
    assert node_local.designated_actuator() == "node-a"
    monkeypatch.setenv("SKOPERATOR_ACTUATOR_NODE", "node-b")
    assert node_local.designated_actuator() == "node-b"


def test_dispatch_intent_refuses_on_a_node_that_is_not_the_actuator(world, monkeypatch):
    from skcapstone.operator_seat import node_local

    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    monkeypatch.setattr(
        node_local, "actuator_refusal", lambda: "this node is not the designated actuator"
    )
    outcome = _dispatch_one(world, world.itil, intent_id, _apply_fn(world))
    assert world.calls == []
    assert outcome.outcome.startswith("refused: this node is not the designated actuator")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert _sweep(world) == []  # run_dispatch_pass stands down entirely


def test_the_loop_refuses_to_actuate_on_a_node_that_is_not_the_actuator(world, monkeypatch):
    from skcapstone.operator_seat import node_local

    monkeypatch.setattr(
        node_local, "actuator_refusal", lambda: "this node is not the designated actuator"
    )
    res = _loop_pass(world)
    assert world.calls == []
    assert res["outcomes"][0]["outcome"].startswith("refused: this node is not the designated")
    assert world.ledger.list_intents() == []


# --------------------------------------------------------------------------
# 5: crashes and killed loops leave nothing behind
# --------------------------------------------------------------------------


def test_an_intent_stranded_in_authorized_retires_its_still_approved_change(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    long_ago = world.time.now - timedelta(minutes=30)
    world.ledger.append(
        intent_id, action_ledger.ActionState.AUTHORIZED, occurred_at=long_ago, actor="crashed"
    )
    outcomes = _sweep(world, decisions_dir=str(world.decisions))
    assert world.calls == []
    assert any(o.intent_id == intent_id and "stranded" in o.outcome for o in outcomes)
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    final = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert final.status.value in ("rejected", "closed"), "an approved change must not stay open"


def test_a_leftover_auto_lane_intent_from_a_killed_loop_is_voided_before_anything_else(world):
    change = _standard_bound_change(world)
    intent_id = _manual_intent(world, change.id, lane="auto", evidence=_stamp(world))
    world.time.advance(minutes=20)  # well over one pass
    outcomes = _sweep(world, max_evidence_age_s=3600)  # evidence alone would still pass
    assert world.calls == []
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    assert not any(o.outcome == "verified" for o in outcomes)


def test_the_loop_voids_a_leftover_auto_lane_intent_at_the_start_of_a_pass(world):
    change = _standard_bound_change(world)
    intent_id = _manual_intent(world, change.id, lane="auto", evidence=_stamp(world))
    world.time.advance(minutes=20)
    _loop_pass(world, target_known=lambda p: False)
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    assert world.calls == []


def test_a_fresh_auto_lane_intent_is_not_voided(world):
    change = _standard_bound_change(world)
    intent_id = _manual_intent(world, change.id, lane="auto", evidence=_stamp(world))
    outcomes = _sweep(world)
    assert outcomes[0].outcome == "verified"
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED
