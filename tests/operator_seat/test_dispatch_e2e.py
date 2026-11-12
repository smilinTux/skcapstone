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


def _manual_intent(w, change_id, *, lane, evidence=None):
    created = w.time.now
    intent = action_ledger.ActionIntent(
        condition_fingerprint=f"fp-e2e-{change_id}",
        application="skchat",
        target_kind="CI",
        target_id="telegram-bridge",
        action="restart-telegram-bridge",
        catalog_generation="e2e",
        created_at=created,
        itil_change_id=change_id,
        condition_type="BridgeAlive",
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


def _normal_change(w, *, tags=("operator",), created_by="atlas"):
    return w.itil.propose_change(
        title="restart telegram bridge",
        change_type="normal",
        risk="low",
        rollback_plan="restart again",
        created_by=created_by,
        tags=list(tags),
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
        tags=["operator"],
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
    outcomes = _sweep(world)
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
    outcomes = _sweep(world)
    assert outcomes[0].outcome == "verified"
    assert len(world.calls) == 1
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED


def test_a_human_approved_intent_days_later_does_not_act_on_a_cleared_condition(world):
    change = _normal_change(world)
    _vote(world, change.id)
    intent_id = _manual_intent(world, change.id, lane="escalate")
    world.time.advance(days=3)
    world.heal()  # it fixed itself while the approval sat
    outcomes = _sweep(world)
    assert world.calls == []
    assert "no longer firing" in outcomes[0].outcome
    # A human-approved intent is not voided: it may legitimately fire again.
    assert world.ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


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
