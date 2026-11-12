"""End to end through ITIL, the ledger and ``dispatch_intent``, with the real
``act_dispatch.build_apply_fn`` and a real adapter (skchat), no stubbed stage.

Review round 3 on #1357: an ESCALATED proposal still got an auto-approved ``standard``
change and was actuated by ``run_dispatch_pass`` with no human vote (A); the age rule
then voided the same pending intent, so a later CAB vote did nothing (B); ``Unknown``
counted as "still firing" at re-observation (C); and the fold's ``auto-normal`` tier,
which also auto-approves with no human, was treated as human-approved (D).

Time is controlled: ``adapter.probe_time`` and the injected ``clock`` follow one
``_Time``, so "three days later" is real for the re-observation's own freshness check.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.fleet import store
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import (
    act_dispatch,
    action_ledger,
    adapter,
    dispatch,
    loop,
    policy,
    proposer,
    skchat_adapter,
)

from .test_loop import _enroll

HUMAN = store.Writer(role="operator", node="node-158", identity="chef")
_REPLY = json.dumps(
    [
        {
            "app": "skchat",
            "condition": "BridgeAlive",
            "action": "restart-telegram-bridge",
            "object": "telegram-bridge",
            "change_class": "normal",
            "rationale": "bridge poll heartbeat is stale while the daemon is up",
        }
    ]
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Time:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class _World:
    pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = _World()
    w.paths, _ = _enroll(tmp_path, monkeypatch)
    store.set_frozen(w.paths, False, writer=HUMAN, reason="test provisioning")
    w.heartbeat = tmp_path / "telegram_poll.ts"
    w.wedge()
    monkeypatch.setenv("SKCHAT_BRIDGE_HEARTBEAT", str(w.heartbeat))
    monkeypatch.setenv("SKCOMMS_OUTBOX_DIR", str(tmp_path / "outbox"))
    w.daemon = {"probe": (True, True, True)}
    monkeypatch.setattr(skchat_adapter, "_probe_daemon_health", lambda: w.daemon["probe"])
    w.time = _Time()
    monkeypatch.setattr(adapter, "probe_time", lambda: _iso(w.time.now))
    monkeypatch.setattr(loop, "ADAPTERS", {"skchat": skchat_adapter.observe})
    # The bridge restart is a RATIFIED STANDARD action in this world, so a proposal for
    # it classifies standard: the class whose ITIL change auto-approves at the fold.
    monkeypatch.setattr(
        policy,
        "RATIFIED_STANDARD_CATALOG",
        policy.RATIFIED_STANDARD_CATALOG | {"restart-telegram-bridge"},
    )
    w.calls = []

    def runner(cmd):
        w.calls.append(cmd)
        w.heal()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    w.runner = runner
    w.itil = ITILManager(tmp_path / "itil")
    w.ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    w.decisions = tmp_path / "decisions"
    w.wedge = w.wedge
    return w


def _wedge(self):
    self.heartbeat.write_text("stale")
    old = time.time() - 1200
    os.utime(self.heartbeat, (old, old))


def _heal(self):
    self.heartbeat.write_text("fresh")
    now = time.time()
    os.utime(self.heartbeat, (now, now))


_World.wedge = _wedge
_World.heal = _heal


def _apply_fn(w):
    return act_dispatch.build_apply_fn(w.paths, _iso(w.time.now), runner=w.runner, itil=w.itil)


def _loop_pass(w, *, target_known=None, standard=True):
    explain = act_dispatch.merged_explain()
    for entry in explain["actions"]:
        if entry["name"] == "restart-telegram-bridge":
            entry["standard"] = standard
            entry["reversible"] = True
    return loop.run_once(
        w.paths,
        now_iso=_iso(w.time.now),
        propose=lambda b, r: proposer.propose(b, explain, chat=lambda _p: _REPLY),
        explain=explain,
        decisions_dir=str(w.decisions),
        apply_fn=_apply_fn(w),
        execute=True,
        lifecycle_ledger=w.ledger,
        itil=w.itil,
        target_known=target_known,
        clock=lambda: w.time.now,
        emit=lambda _s: None,
    )


def _sweep(w, **kwargs):
    return dispatch.run_dispatch_pass(
        w.paths,
        w.ledger,
        w.itil,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=_apply_fn(w),
        now_iso=_iso(w.time.now),
        clock=lambda: w.time.now,
        **kwargs,
    )


def _change(w, intent_id):
    change_id = w.ledger.read_intent(intent_id).itil_change_id
    return next(c for c in w.itil.list_changes() if c.id == change_id)


def _vote(w, change_id):
    w.itil.submit_cab_vote(
        change_id,
        agent="capauth:approver@scratch",
        decision="approved",
        subject="capauth:approver@scratch",
        subject_role="approver",
    )


def _escalated_intent(w, *, standard=True):
    """A proposal the planner escalates (its target does not resolve), via the real chain."""
    res = _loop_pass(w, target_known=lambda p: False, standard=standard)
    outcome = res["outcomes"][0]
    assert outcome["outcome"] == "escalated (parked for approval)"
    assert w.calls == []
    return outcome["intent_id"]


def _manual_intent(
    w,
    change_id,
    *,
    lane,
    evidence=None,
    action="restart-telegram-bridge",
    rollback=None,
    tag="",
):
    created = w.time.now
    intent = action_ledger.ActionIntent(
        condition_fingerprint=f"fp-e2e-{change_id}{tag}",
        application="skchat",
        target_kind="CI",
        target_id="telegram-bridge",
        action=action,
        catalog_generation="e2e",
        created_at=created,
        itil_change_id=change_id,
        condition_type="BridgeAlive",
        rollback=rollback or {},
    )
    w.ledger.create(intent, actor="test")
    w.ledger.append(
        intent.intent_id, action_ledger.ActionState.DIAGNOSED, occurred_at=created, actor="test"
    )
    detail = {}
    if lane:
        detail["disposition"] = lane
    if evidence:
        detail["evidence"] = evidence
    w.ledger.append(
        intent.intent_id,
        action_ledger.ActionState.PROPOSED,
        occurred_at=created,
        actor="test",
        detail=detail,
    )
    return intent.intent_id


BIND = "op-bind:skchat|restart-telegram-bridge|telegram-bridge"
WEEK = 604800.0  # the configurable cap; the default human approval age is a day


def _normal_change(w, *, tags=("operator",), created_by="atlas", bind=BIND):
    return w.itil.propose_change(
        title="restart telegram bridge",
        change_type="normal",
        risk="low",
        rollback_plan="restart again",
        created_by=created_by,
        tags=[*tags, bind],
    )


# --------------------------------------------------------------------------
# A: an escalated proposal never gets an auto-approving change
# --------------------------------------------------------------------------


def test_an_escalated_proposal_gets_a_cab_gated_change_not_an_auto_approved_one(world):
    intent_id = _escalated_intent(world)
    change = _change(world, intent_id)
    assert change.change_type.value == "normal"
    assert change.status.value == "proposed"
    assert not any(e.get("action") == "auto-approved" for e in change.timeline)


def test_an_escalated_proposal_is_not_actuated_by_a_later_dispatch_pass_without_a_human(world):
    intent_id = _escalated_intent(world)
    world.time.advance(seconds=60)
    outcomes = _sweep(world)
    assert world.calls == [], "the actuator must never run without a human vote"
    assert outcomes[0].outcome.startswith("pending")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_the_escalate_lane_never_carries_the_auto_normal_tag(world):
    intent_id = _escalated_intent(world, standard=False)
    assert "auto-normal" not in _change(world, intent_id).tags


def test_a_legacy_auto_approved_change_on_an_escalated_intent_is_refused_not_voided(world):
    """What the old loop wrote: a standard (auto-approving) change behind an escalation."""
    change = world.itil.propose_change(
        title="restart telegram bridge",
        change_type="standard",
        risk="low",
        rollback_plan="restart again",
        created_by="atlas",
        tags=["operator", BIND],
    )
    intent_id = _manual_intent(world, change.id, lane="escalate", evidence=_stamp(world))
    world.time.advance(hours=2)
    outcomes = _sweep(world)
    assert world.calls == []
    assert outcomes[0].outcome.startswith("pending")
    assert "no human approval" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# --------------------------------------------------------------------------
# B: the age rule never voids a pending human decision
# --------------------------------------------------------------------------


def test_a_pending_human_decision_survives_two_hours_and_a_later_cab_vote_still_works(world):
    intent_id = _escalated_intent(world)
    world.time.advance(hours=2)
    first = _sweep(world)
    assert first[0].outcome.startswith("pending")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    _vote(world, world.ledger.read_intent(intent_id).itil_change_id)
    second = _sweep(world)
    assert second[0].outcome == "verified"
    assert len(world.calls) == 1
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def test_dispatch_rechecks_the_target_before_acting_on_a_human_approval(world):
    intent_id = _escalated_intent(world)
    _vote(world, world.ledger.read_intent(intent_id).itil_change_id)
    outcomes = _sweep(world, target_known=lambda p: False)
    assert world.calls == []
    assert "target" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_dispatch_rechecks_the_action_binding_before_acting_on_a_human_approval(world):
    intent_id = _escalated_intent(world)
    _vote(world, world.ledger.read_intent(intent_id).itil_change_id)
    outcomes = _sweep(world, action_allowed=lambda p: False)
    assert world.calls == []
    assert "not allowed" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# --------------------------------------------------------------------------
# C: Unknown is not "still firing"
# --------------------------------------------------------------------------


def test_an_unknown_status_at_re_observation_does_not_act_and_keeps_the_intent(world):
    intent_id = _escalated_intent(world)
    _vote(world, world.ledger.read_intent(intent_id).itil_change_id)
    world.time.advance(days=3)
    world.daemon["probe"] = (None, None, None)  # the daemon cannot be probed: Unknown
    outcomes = _sweep(world, max_approval_age_s=WEEK)
    assert world.calls == []
    assert "Unknown" in outcomes[0].outcome or "unknown" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_a_re_observation_with_no_source_time_does_not_act(world, monkeypatch):
    intent_id = _escalated_intent(world)
    _vote(world, world.ledger.read_intent(intent_id).itil_change_id)

    def unstamped(paths=None, now_iso=None):
        payload = skchat_adapter.skchat_observe()
        payload.pop("source_observed_at", None)
        return payload

    outcomes = dispatch.run_dispatch_pass(
        world.paths,
        world.ledger,
        world.itil,
        adapters={"skchat": unstamped},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=_apply_fn(world),
        now_iso=_iso(world.time.now),
        clock=lambda: world.time.now,
    )
    assert world.calls == []
    assert "source time" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# --------------------------------------------------------------------------
# human-approved intents days later
# --------------------------------------------------------------------------


def test_a_human_approved_intent_days_later_acts_when_the_condition_is_definitely_firing(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.time.advance(days=3)
    outcomes = _sweep(world, max_approval_age_s=WEEK)
    assert outcomes[0].outcome == "verified"
    assert len(world.calls) == 1
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def test_a_human_approved_intent_days_later_does_not_act_on_a_cleared_condition(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.time.advance(days=3)
    world.heal()  # it fixed itself while the approval sat
    outcomes = _sweep(world, max_approval_age_s=WEEK)
    assert world.calls == []
    assert "condition cleared before dispatch" in outcomes[0].outcome
    # A human approval covers ONE episode: it is closed with the episode, and the change is
    # retired, so a recurrence cannot ride the old vote.
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    retired = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert retired.status.value in ("rejected", "closed")
    world.wedge()  # it fires again
    assert _sweep(world) == [] and world.calls == []


# --------------------------------------------------------------------------
# D: the fold's auto-normal tier is auto-approval too
# --------------------------------------------------------------------------


def test_an_auto_normal_change_is_treated_as_auto_approved_not_human_approved(world):
    change = _normal_change(world, tags=("operator", "auto-normal"), created_by="operator")
    assert any(e.get("action") == "auto-approved" for e in change.timeline) or (
        next(c for c in world.itil.list_changes() if c.id == change.id).status.value == "approved"
    )
    intent_id = _manual_intent(world, change.id, lane="auto")  # no stored evidence at all
    world.time.advance(days=1)
    outcomes = _sweep(world)
    assert world.calls == []
    assert "evidence missing" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED


def test_an_auto_approved_change_with_fresh_stored_evidence_still_acts(world):
    change = _normal_change(world, tags=("operator", "auto-normal"), created_by="operator")
    intent_id = _manual_intent(world, change.id, lane="auto", evidence=_stamp(world))
    outcomes = _sweep(world)
    assert outcomes[0].outcome == "verified"
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def _stamp(w):
    return {
        "source": "observation:skchat/BridgeAlive/telegram-bridge",
        "observed_at": _iso(w.time.now),
    }


# ==========================================================================
# Round 4 (third independent review of 315300c0)
# ==========================================================================


def _schedule_and_unschedule(w, change_id):
    """approved -> scheduled -> approved: the fold stays approved but the
    ``auto-approved`` timeline marker is gone (it is only written while PROPOSED)."""
    when = _iso(w.time.now)
    w.itil._append_event(
        w.itil.changes_dir,
        change_id,
        "capauth:approver@scratch",
        "schedule",
        window_start=when,
        window_end=when,
        asap=True,
        deploy_mode="confirm",
    )
    w.itil._append_event(
        w.itil.changes_dir, change_id, "capauth:approver@scratch", "unschedule", note="rescheduled"
    )
    change = next(c for c in w.itil.list_changes() if c.id == change_id)
    assert change.status.value == "approved"
    assert not any(e.get("action") == "auto-approved" for e in change.timeline)
    return change


def _standard_bound_change(w):
    return w.itil.propose_change(
        title="restart telegram bridge",
        change_type="standard",
        risk="low",
        rollback_plan="restart again",
        created_by="atlas",
        tags=["operator", BIND],
    )


def _states(w, intent_id):
    return [e.state.value for e in w.ledger.events(intent_id)]


# --- 1: "human-approved" needs a qualifying human vote, not a missing marker ----


def test_a_rescheduled_standard_change_is_still_not_human_approved_for_an_escalated_intent(world):
    change = _standard_bound_change(world)
    _schedule_and_unschedule(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate", evidence=_stamp(world))
    outcomes = _sweep(world)
    assert world.calls == []
    assert outcomes[0].outcome.startswith("pending")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_a_rescheduled_standard_change_is_not_human_approved_for_a_legacy_no_lane_intent(world):
    change = _standard_bound_change(world)
    _schedule_and_unschedule(world, change.id)
    intent_id = _manual_intent(world, change.id, lane=None, evidence=_stamp(world))
    outcomes = _sweep(world)
    assert world.calls == []
    assert "no recorded auto lane" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_a_rescheduled_standard_change_still_needs_fresh_evidence_for_an_auto_intent(world):
    change = _standard_bound_change(world)
    _schedule_and_unschedule(world, change.id)
    ancient = {
        "source": "observation:skchat/BridgeAlive/telegram-bridge",
        "observed_at": "2019-01-01T00:00:00Z",
    }
    intent_id = _manual_intent(world, change.id, lane="auto", evidence=ancient)
    outcomes = _sweep(world)
    assert world.calls == []
    assert "evidence stale" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED


def test_a_rescheduled_auto_normal_change_is_not_human_approved(world):
    change = _normal_change(world, tags=("operator", "auto-normal"), created_by="operator")
    _schedule_and_unschedule(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert world.calls == []
    assert outcomes[0].outcome.startswith("pending")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_a_rescheduled_change_that_a_human_really_approved_still_acts(world):
    change = _normal_change(world)
    _vote(world, change.id)
    _schedule_and_unschedule(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert outcomes[0].outcome == "verified"
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def test_a_human_rejection_beats_an_approval(world):
    change = _normal_change(world)
    _vote(world, change.id)
    world.itil.submit_cab_vote(
        change.id,
        agent="capauth:other@scratch",
        decision="rejected",
        subject="capauth:other@scratch",
        subject_role="approver",
    )
    intent_id = _manual_intent(world, change.id, lane="escalate")
    _sweep(world)
    assert world.calls == []
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# --- 2: the change is bound to the intent, and used up ---------------------------


def test_a_change_approved_for_one_action_does_not_authorize_another(world):
    change = _normal_change(world)  # bound to restart-telegram-bridge
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate", action="restart-daemon")
    outcomes = _sweep(world)
    assert world.calls == [], "a change approved for one action must not run another"
    assert "not bound" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


def test_a_change_with_no_binding_authorizes_nothing(world):
    change = world.itil.propose_change(
        title="restart telegram bridge",
        change_type="normal",
        risk="low",
        rollback_plan="restart again",
        created_by="atlas",
        tags=["operator"],
    )
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert world.calls == []
    assert "not bound" in outcomes[0].outcome


def test_a_change_is_used_up_by_a_verified_run(world):
    change = _normal_change(world)
    _vote(world, change.id)
    first = _manual_intent(world, change.id, lane="escalate", tag="-1")
    assert _sweep(world)[0].outcome == "verified"
    assert len(world.calls) == 1
    done = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert done.status.value == "deployed"
    # A second intent bound to the SAME change, with the condition firing again.
    world.wedge()
    second = _manual_intent(world, change.id, lane="escalate", tag="-2")
    outcomes = [o for o in _sweep(world) if o.intent_id == second]
    assert len(world.calls) == 1, "an approval is spent by the run it authorized"
    assert outcomes[0].outcome.startswith("pending: ITIL change status=deployed")
    assert world.ledger.current_state(first) is action_ledger.ActionState.VERIFIED


def test_a_change_is_closed_out_as_failed_when_the_run_fails(world):
    change = _normal_change(world)
    _vote(world, change.id)
    world.runner = lambda cmd: subprocess.CompletedProcess(cmd, 0, "", "")  # never heals
    _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert outcomes[0].outcome.startswith("failed")
    failed = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert failed.status.value == "failed"


def test_the_loop_binds_each_drafted_change_to_its_intent(world):
    intent_id = _escalated_intent(world)
    change = _change(world, intent_id)
    assert BIND in change.tags
    assert [t for t in change.tags if t.startswith("op-bind:")] == [BIND]


def test_a_proposal_supplied_change_id_is_never_trusted(world):
    approved = _normal_change(world, bind="op-bind:skchat|something-else|elsewhere")
    _vote(world, approved.id)
    explain = act_dispatch.merged_explain()
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
                "change_id": approved.id,
                "itil_change_id": approved.id,
            }
        ],
        explain=explain,
        decisions_dir=str(world.decisions),
        apply_fn=_apply_fn(world),
        execute=True,
        lifecycle_ledger=world.ledger,
        itil=world.itil,
        target_known=lambda p: False,
        clock=lambda: world.time.now,
        emit=lambda _s: None,
    )
    intent_id = res["outcomes"][0]["intent_id"]
    bound = world.ledger.read_intent(intent_id).itil_change_id
    assert bound != approved.id, "the loop drafts its own change, whatever the proposal says"
    assert world.calls == []


# --- 3: suppression is a refusal, not a failure ----------------------------------


def test_a_cooldown_is_a_refusal_with_no_failure_record_and_no_rollback(world, tmp_path):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(
        world,
        change.id,
        lane="escalate",
        rollback={"action": "restart-daemon"},
    )
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=900)
    fingerprint = loop.safety.action_fingerprint(
        {
            "app": "skchat",
            "condition": "BridgeAlive",
            "object": "telegram-bridge",
            "action": "restart-telegram-bridge",
        }
    )
    state.record(fingerprint, time.time(), success=False, reason="earlier failure")
    rollbacks = []
    outcomes = _sweep(
        world,
        execution_state=state,
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
    )
    assert world.calls == []
    assert outcomes[0].outcome.startswith("refused: execution suppressed")
    assert rollbacks == [], "nothing was attempted, so there is nothing to roll back"
    assert state._load()["actions"][fingerprint]["consecutive_failures"] == 1
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert "authorized" not in _states(world, intent_id)


# --- 4: stale catalog never runs before the lane and human checks ---------------


def _make_catalog_stale(w):
    for _ in range(2):  # generation 2; the intents below were proposed against "e2e"
        store.write_spec(
            w.paths, "operatorapp", "skchat", {"ratifiedStandardActions": []}, writer=HUMAN
        )


def test_a_stale_catalog_refuses_a_human_approved_intent_without_authorizing_it(world):
    change = _normal_change(world)
    _vote(world, change.id)
    _make_catalog_stale(world)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert world.calls == []
    assert "stale catalog_generation" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert "authorized" not in _states(world, intent_id)


def test_a_stale_catalog_does_not_close_an_escalate_lane_intent_with_no_human(world):
    change = _standard_bound_change(world)
    _make_catalog_stale(world)
    intent_id = _manual_intent(world, change.id, lane="escalate", evidence=_stamp(world))
    outcomes = _sweep(world)
    assert world.calls == []
    assert outcomes[0].outcome.startswith("pending")
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert _states(world, intent_id) == ["observed", "diagnosed", "proposed"]


def test_a_stale_catalog_does_not_close_a_legacy_no_lane_intent(world):
    change = _standard_bound_change(world)
    _make_catalog_stale(world)
    intent_id = _manual_intent(world, change.id, lane=None, evidence=_stamp(world))
    _sweep(world)
    assert world.calls == []
    assert _states(world, intent_id) == ["observed", "diagnosed", "proposed"]


# ==========================================================================
# Round 5 (fourth independent review of 8536f7eb)
# ==========================================================================


class _SlowITIL:
    """Delegates to a real ITILManager; ``update_change`` can sleep and run a hook first."""

    def __init__(self, inner, delay=0.0, before_update=None):
        self._inner = inner
        self._delay = delay
        self._before_update = before_update

    def update_change(self, *args, **kwargs):
        if self._before_update is not None:
            hook, self._before_update = self._before_update, None
            hook()
        if self._delay:
            time.sleep(self._delay)
        return self._inner.update_change(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _dispatch_one(w, itil, intent_id, apply_fn):
    return dispatch.dispatch_intent(
        w.paths,
        w.ledger,
        itil,
        intent_id,
        adapters={"skchat": skchat_adapter.observe},
        problem_types=loop.PROBLEM_WHEN_TRUE,
        apply_fn=apply_fn,
        now_iso=_iso(w.time.now),
        clock=lambda: w.time.now,
    )


# --- 1: spending the approval is a compare-and-swap ------------------------------


def test_two_dispatchers_racing_on_one_approval_actuate_once(world):
    change = _normal_change(world)
    _vote(world, change.id)
    first = _manual_intent(world, change.id, lane="escalate", tag="-a")
    second = _manual_intent(world, change.id, lane="escalate", tag="-b")
    slow = _SlowITIL(world.itil, delay=0.3)
    applied = []
    lock = threading.Lock()

    def counting_apply(prop, classification):
        with lock:
            applied.append(prop["action"])
        return _apply_fn(world)(prop, classification)

    results = {}

    def run(name, intent_id):
        results[name] = _dispatch_one(world, slow, intent_id, counting_apply)

    threads = [
        threading.Thread(target=run, args=("a", first)),
        threading.Thread(target=run, args=("b", second)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(applied) == 1, "one approval authorizes one actuation, even under a race"
    assert len(world.calls) == 1
    outcomes = sorted(r.outcome.split(":")[0] for r in results.values())
    assert "verified" in outcomes and len(outcomes) == 2
    final = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert final.status.value == "deployed", "the ITIL record must agree with the ledger"


def test_a_rival_dispatch_that_took_the_change_first_is_detected_from_the_timeline(world):
    """Even a conflicted second 'implementing' event folds ``implementing``: the winner is
    the FIRST unconflicted approved->implementing entry, and it must be ours."""
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")

    def rival_takes_it():
        world.itil._append_event(
            world.itil.changes_dir,
            change.id,
            "rival",
            "status",
            to="implementing",
            note="dispatch of ai-someone-else",
        )

    slow = _SlowITIL(world.itil, before_update=rival_takes_it)
    applied = []
    outcome = _dispatch_one(
        world, slow, intent_id, lambda p, c: applied.append(p) or {"performed": True}
    )
    assert applied == [], "the actuator must not run on an approval another dispatch spent"
    assert "taken" in outcome.outcome or "status=" in outcome.outcome


# --- 2: a human approval is per episode, and it expires ---------------------------


def test_a_cleared_episode_retires_the_change_so_a_recurrence_needs_a_new_vote(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.heal()
    outcomes = _sweep(world)
    assert "condition cleared before dispatch" in outcomes[0].outcome
    states = _states(world, intent_id)
    assert states[-1] == "escalated"
    last = world.ledger.events(intent_id)[-1]
    assert "condition cleared before dispatch" in last.detail.get("reason", "")
    retired = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert retired.status.value in ("rejected", "closed")


def test_a_human_approval_within_the_age_limit_acts(world):
    change = _normal_change(world)
    _vote(world, change.id)
    _manual_intent(world, change.id, lane="escalate")
    world.time.advance(hours=23)
    assert _sweep(world)[0].outcome == "verified"


def test_an_expired_human_approval_is_refused_and_the_intent_closed(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.time.advance(hours=25)  # past the 24 hour default
    outcomes = _sweep(world)
    assert world.calls == []
    assert "human approval expired" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    retired = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert retired.status.value in ("rejected", "closed")


def test_the_approval_age_limit_is_configurable_and_capped(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.time.advance(hours=2)
    tight = _sweep(world, max_approval_age_s=3600)
    assert world.calls == [] and "human approval expired" in tight[0].outcome
    # A value above the cap is refused (fail closed), never clamped, and closes nothing.
    change2 = _normal_change(world)
    _vote(world, change2.id)
    other = _manual_intent(world, change2.id, lane="escalate", tag="-2")
    capped = _sweep(world, max_approval_age_s=10**9)
    assert world.calls == []
    assert any("max approval age" in o.outcome for o in capped if o.intent_id == other)
    assert world.ledger.current_state(other) is action_ledger.ActionState.PROPOSED
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED


def test_a_future_dated_approval_is_refused(world):
    change = _normal_change(world)
    _vote(world, change.id)
    path = world.itil.cab_dir / f"{change.id}-capauth:approver@scratch.json"
    data = json.loads(path.read_text())
    data["decided_at"] = _iso(world.time.now + timedelta(days=30))
    path.write_text(json.dumps(data))
    intent_id = _manual_intent(world, change.id, lane="escalate")
    outcomes = _sweep(world)
    assert world.calls == []
    assert "future" in outcomes[0].outcome
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# --- 3: the escalate lane does not draft again on every pass ----------------------


def test_an_open_escalated_intent_is_reused_not_redrafted_each_pass(world):
    first = _escalated_intent(world)
    changes_before = len(world.itil.list_changes())
    again = _loop_pass(world, target_known=lambda p: False)
    assert again["outcomes"][0]["outcome"] == "escalated (parked for approval)"
    assert again["outcomes"][0]["intent_id"] == first
    assert len(world.itil.list_changes()) == changes_before, "no new change on every pass"
    assert world.ledger.list_intents() == [first]


def test_a_retired_escalated_intent_is_not_reused(world):
    first = _escalated_intent(world)
    change_id = world.ledger.read_intent(first).itil_change_id
    world.itil.update_change(change_id, "test", new_status="rejected", note="not wanted")
    again = _loop_pass(world, target_known=lambda p: False)
    assert again["outcomes"][0]["intent_id"] != first


# --- 4: the binding tag cannot be forged by a separator ---------------------------


def test_binding_tags_are_unambiguous_and_reject_the_separator():
    tag = dispatch.change_binding_tag
    with pytest.raises(ValueError):
        tag("skchat", "restart|x", "bridge")
    with pytest.raises(ValueError):
        tag("a|b", "c", "d")
    triples = [
        ("a", "b", "c"),
        ("a%7Cb", "c", "d"),
        ("a%257Cb", "c", "d"),
        ("a b", "c", "d"),
        ("a", "b c", "d"),
        ("", "b", "c"),
        ("a", "", "bc"),
    ]
    tags = [tag(*t) for t in triples]
    assert len(set(tags)) == len(tags)


def test_an_object_containing_the_separator_is_refused_by_the_loop_before_any_write(world):
    explain = act_dispatch.merged_explain()
    res = loop.run_once(
        world.paths,
        now_iso=_iso(world.time.now),
        propose=lambda b, r: [
            {
                "app": "skchat",
                "condition": "BridgeAlive",
                "action": "restart-telegram-bridge",
                "object": "telegram-bridge|elsewhere",
                "evidence": _stamp(world),
            }
        ],
        explain=explain,
        decisions_dir=str(world.decisions),
        apply_fn=_apply_fn(world),
        execute=True,
        lifecycle_ledger=world.ledger,
        itil=world.itil,
        clock=lambda: world.time.now,
        emit=lambda _s: None,
    )
    assert world.calls == []
    assert res["outcomes"][0]["outcome"].startswith("refused: ")
    assert "cannot be bound" in res["outcomes"][0]["outcome"]
    assert world.ledger.list_intents() == []
    assert world.itil.list_changes() == []


# --- 5: one bad intent does not crash the pass; stranded intents escalate ---------


class _FailingLedger(action_ledger.ActionLedger):
    def __init__(self, root, fail_for):
        super().__init__(root)
        self._fail_for = fail_for

    def append(self, intent_id, state, **kwargs):
        if intent_id == self._fail_for and state is action_ledger.ActionState.AUTHORIZED:
            raise ValueError("invalid action transition: simulated")
        return super().append(intent_id, state, **kwargs)


def test_a_ledger_error_on_one_intent_is_recorded_and_the_pass_continues(world, tmp_path):
    bad_change = _normal_change(world)
    _vote(world, bad_change.id)
    bad = _manual_intent(world, bad_change.id, lane="escalate", tag="-bad")
    good_change = _normal_change(world)
    _vote(world, good_change.id)
    good = _manual_intent(world, good_change.id, lane="escalate", tag="-good")
    world.ledger = _FailingLedger(tmp_path / "ledger", fail_for=bad)
    # The runner does not heal, so the condition stays firing whichever intent runs first.
    world.runner = lambda cmd: world.calls.append(cmd) or subprocess.CompletedProcess(
        cmd, 0, "", ""
    )
    outcomes = {o.intent_id: o.outcome for o in _sweep(world)}
    assert outcomes[bad].startswith("error:")
    assert good in outcomes, "the pass carried on to the next intent"
    assert not outcomes[good].startswith("error:")


def _strand(w, change_id, *, age):
    """An intent a crashed dispatcher left in EXECUTING, with its change `implementing`."""
    intent_id = _manual_intent(w, change_id, lane="escalate")
    when = w.time.now - age
    w.ledger.append(
        intent_id, action_ledger.ActionState.AUTHORIZED, occurred_at=when, actor="crashed"
    )
    w.ledger.append(
        intent_id, action_ledger.ActionState.EXECUTING, occurred_at=when, actor="crashed"
    )
    w.itil.update_change(change_id, "crashed", new_status="implementing", note="dispatch of x")
    return intent_id


def test_an_intent_stranded_in_executing_is_escalated_with_an_alert_never_rerun(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _strand(world, change.id, age=timedelta(minutes=30))
    alerts = []
    outcomes = _sweep(world, decisions_dir=str(world.decisions), emit=alerts.append)
    assert world.calls == [], "a stranded intent is never re-run"
    assert any(o.intent_id == intent_id and "stranded" in o.outcome for o in outcomes)
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    assert any("stranded" in a and intent_id in a for a in alerts)
    assert [d for d in decisions_pending(world) if "stranded" in d["id"]]
    final = next(c for c in world.itil.list_changes() if c.id == change.id)
    assert final.status.value == "failed"


def decisions_pending(w):
    from skcapstone.operator_seat import decisions

    return decisions.list_pending(str(w.decisions))


def test_a_recently_started_executing_intent_is_left_alone(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _strand(world, change.id, age=timedelta(seconds=20))
    _sweep(world, decisions_dir=str(world.decisions))
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.EXECUTING
    assert world.calls == []


def test_the_loop_escalates_a_stranded_intent_and_reports_it(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _strand(world, change.id, age=timedelta(minutes=30))
    res = _loop_pass(world, target_known=lambda p: False)
    assert intent_id in res["stranded"]
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.ESCALATED
    assert "stranded" in res["report"]
