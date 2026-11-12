"""A PROPOSED ledger intent must never become an actuation later, unattended.

Review finding 2 on #1357: when the loop wrote the ITIL change and the ledger intent
and then the dispatcher refused (a blocked gate, a freeze landing in between, the
deadline), the intent stayed PROPOSED. ``run_dispatch_pass`` later acted on it, with
no evidence check and no human, because an auto-approved standard change reads
approved. These tests pin the three layers of the fix: refuse before writing,
settle what was written, and make the dispatcher itself demand fresh evidence or a
re-observation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.fleet import store
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import action_ledger, dispatch, loop

from .test_loop import _enroll

HUMAN = store.Writer(role="operator", node="node-158", identity="chef")
CLOCK = datetime(2026, 8, 20, 0, 0, 30, tzinfo=timezone.utc)
NOW = "2026-08-20T00:00:00Z"
FRESH = {"source": "obs:demo:svc", "observed_at": "2026-08-20T00:00:00Z"}


@pytest.fixture(autouse=True)
def _votes_decided_before_the_test_clock(monkeypatch):
    """CAB votes record the REAL time they were cast. These tests run on a fixed clock in
    the past, and an approval dated after the clock is (rightly) refused as future-dated,
    so each vote is re-dated to just before the clock under test."""
    import json as _json

    original = ITILManager.submit_cab_vote

    def patched(self, change_id, *args, **kwargs):
        vote = original(self, change_id, *args, **kwargs)
        path = self.cab_dir / f"{change_id}-{vote.agent}.json"
        data = _json.loads(path.read_text())
        data["decided_at"] = "2026-08-20T12:00:00+00:00"
        path.write_text(_json.dumps(data))
        return vote

    monkeypatch.setattr(ITILManager, "submit_cab_vote", patched)


class _CountingITIL:
    """Delegates to a real ITILManager, counting (and optionally hooking) drafts."""

    def __init__(self, inner, on_draft=None):
        self._inner = inner
        self.drafted = 0
        self._on_draft = on_draft

    def propose_change(self, *args, **kwargs):
        self.drafted += 1
        change = self._inner.propose_change(*args, **kwargs)
        if self._on_draft is not None:
            self._on_draft()
        return change

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _Healable:
    """A demo-app observer: firing (Ready=False) until the action heals it.

    A live observer stamps the time it read: ``stamp`` follows the clock under test.
    """

    def __init__(self):
        self.healed = False
        self.status_override = None
        self.stamp = "2026-08-20T00:00:30Z"

    def __call__(self, paths, now_iso):
        status = self.status_override or ("True" if self.healed else "False")
        return {
            "source_observed_at": self.stamp,
            "conditions": [{"type": "Ready", "status": status, "object": "svc"}],
        }


class _Apply:
    def __init__(self, observer):
        self.observer = observer
        self.calls = []

    def __call__(self, prop, classification):
        self.calls.append(prop)
        self.observer.healed = True
        return {"performed": True}


def _setup(tmp_path, monkeypatch, *, provision=True):
    paths, _ = _enroll(tmp_path, monkeypatch)
    if provision:
        store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")
    observer = _Healable()
    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observer})
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    itil = _CountingITIL(ITILManager(tmp_path / "itil"))
    return paths, observer, ledger, itil


_BIND = "op-bind:demo|restart_service|svc"
_PROP = {"app": "demo", "condition": "Ready", "action": "restart_service", "object": "svc"}
_EXPLAIN = {"actions": [{"name": "restart_service", "standard": True, "reversible": True}]}


def _pass(paths, observer, ledger, itil, *, apply_fn=None, **kwargs):
    apply_fn = apply_fn or _Apply(observer)
    result = loop.run_once(
        paths,
        now_iso=NOW,
        propose=lambda b, r: [{**_PROP, "evidence": dict(FRESH)}],
        explain=_EXPLAIN,
        apply_fn=apply_fn,
        execute=True,
        lifecycle_ledger=ledger,
        itil=itil,
        clock=lambda: CLOCK,
        emit=lambda _s: None,
        **kwargs,
    )
    return result, apply_fn


def _states(ledger):
    return {iid: ledger.current_state(iid) for iid in ledger.list_intents()}


def _dispatch_pass(paths, observer, ledger, itil, apply_fn, *, clock=None, **kwargs):
    clock = clock or (lambda: CLOCK + timedelta(days=1))
    observer.stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")  # a re-observation is fresh NOW
    return dispatch.run_dispatch_pass(
        paths,
        ledger,
        itil,
        adapters={"demo": observer},
        problem_types=set(),  # Ready is a health type: fires when False
        apply_fn=apply_fn,
        now_iso=NOW,
        clock=clock,
        **kwargs,
    )


# --------------------------------------------------------------------------
# layer 1: refuse on ANY blocked gate before writing anything
# --------------------------------------------------------------------------


def test_an_unprovisioned_estate_refuses_before_any_itil_change_or_ledger_intent(
    tmp_path, monkeypatch
):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch, provision=False)
    res, apply_fn = _pass(paths, observer, ledger, itil)
    outcome = res["outcomes"][0]
    assert outcome["outcome"].startswith("refused: ")
    assert "unprovisioned" in outcome["outcome"]
    assert outcome["freeze_state"] == "unprovisioned"
    assert apply_fn.calls == []
    assert itil.drafted == 0
    assert ledger.list_intents() == []


# --------------------------------------------------------------------------
# layer 2: whatever the loop wrote and the dispatcher refused is settled in-pass
# --------------------------------------------------------------------------


def test_a_freeze_between_the_loop_gate_and_the_dispatcher_leaves_no_actionable_intent(
    tmp_path, monkeypatch
):
    paths, observer, ledger, _ = _setup(tmp_path, monkeypatch)
    itil = _CountingITIL(
        ITILManager(tmp_path / "itil"),
        on_draft=lambda: store.set_frozen(paths, True, writer=HUMAN, reason="lands mid-pass"),
    )
    res, apply_fn = _pass(paths, observer, ledger, itil)
    assert apply_fn.calls == []
    assert res["frozen_mid_pass"] is True
    states = _states(ledger)
    assert states, "the intent was written before the freeze landed"
    assert action_ledger.ActionState.PROPOSED not in states.values()
    # The next day, after an operator lifts the freeze, nothing is left to dispatch.
    store.set_frozen(paths, False, writer=HUMAN, reason="lifted")
    later = _Apply(observer)
    assert _dispatch_pass(paths, observer, ledger, itil, later) == []
    assert later.calls == []


def test_the_deadline_firing_after_the_intent_is_written_voids_it(tmp_path, monkeypatch):
    paths, observer, ledger, _ = _setup(tmp_path, monkeypatch)
    armed = {"on": False}
    itil = _CountingITIL(
        ITILManager(tmp_path / "itil"), on_draft=lambda: armed.__setitem__("on", True)
    )

    def deadline(_deadline):
        if armed["on"]:
            raise TimeoutError("Atlas operator pass exceeded its maximum runtime")

    monkeypatch.setattr(loop.safety, "assert_before_deadline", deadline)
    with pytest.raises(TimeoutError):
        _pass(paths, observer, ledger, itil)
    states = _states(ledger)
    assert states
    assert action_ledger.ActionState.PROPOSED not in states.values()
    later = _Apply(observer)
    assert _dispatch_pass(paths, observer, ledger, itil, later) == []
    assert later.calls == []


def test_a_dispatcher_that_did_not_actuate_is_settled_not_left_proposed(tmp_path, monkeypatch):
    """The dispatcher cannot find the drafted change: it refuses, and the loop voids."""
    paths, observer, ledger, _ = _setup(tmp_path, monkeypatch)

    class _LosesTheChange(_CountingITIL):
        def list_changes(self):
            return []

    itil = _LosesTheChange(ITILManager(tmp_path / "itil"))
    res, apply_fn = _pass(paths, observer, ledger, itil)
    assert apply_fn.calls == []
    assert "not found" in res["outcomes"][0]["outcome"]
    assert action_ledger.ActionState.PROPOSED not in _states(ledger).values()


def test_a_cooldown_suppressed_intent_is_not_left_proposed(tmp_path, monkeypatch):
    """The inline lane (no ITIL manager) with a proposer-bound change id."""
    paths, observer, ledger, _ = _setup(tmp_path, monkeypatch)
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=900)
    fingerprint = loop.safety.action_fingerprint(_PROP)
    state.record(fingerprint, __import__("time").time(), success=False, reason="earlier failure")
    res = loop.run_once(
        paths,
        now_iso=NOW,
        propose=lambda b, r: [{**_PROP, "evidence": dict(FRESH), "change_id": "chg-bound"}],
        explain=_EXPLAIN,
        apply_fn=_Apply(observer),
        execute=True,
        lifecycle_ledger=ledger,
        execution_state=state,
        clock=lambda: CLOCK,
        emit=lambda _s: None,
    )
    assert "cooldown" in res["outcomes"][0]["outcome"]
    assert action_ledger.ActionState.PROPOSED not in _states(ledger).values()


# --------------------------------------------------------------------------
# layer 3: the dispatcher itself, for any intent however it was left
# --------------------------------------------------------------------------


def _standard_change(itil):
    return itil.propose_change(
        title="restart svc",
        change_type="standard",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator", _BIND],
    )


def _normal_change_approved_by_a_human(itil):
    chg = itil.propose_change(
        title="restart svc",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator", _BIND],
    )
    itil.submit_cab_vote(
        chg.id,
        agent="capauth:approver@scratch",
        decision="approved",
        subject="capauth:approver@scratch",
        subject_role="approver",
    )
    return chg


def _proposed_intent(ledger, change_id, *, evidence=None, lane="auto"):
    created_at = datetime(2026, 8, 20, tzinfo=timezone.utc)
    intent = action_ledger.ActionIntent(
        condition_fingerprint="fp-demo-restart-svc",
        application="demo",
        target_kind="CI",
        target_id="svc",
        action="restart_service",
        catalog_generation="gen-0",
        created_at=created_at,
        itil_change_id=change_id,
        condition_type="Ready",
    )
    ledger.create(intent, actor="test")
    ledger.append(
        intent.intent_id, action_ledger.ActionState.DIAGNOSED, occurred_at=created_at, actor="test"
    )
    ledger.append(
        intent.intent_id,
        action_ledger.ActionState.PROPOSED,
        occurred_at=created_at,
        actor="test",
        detail={
            **({"disposition": lane} if lane else {}),
            **({"evidence": evidence} if evidence else {}),
        },
    )
    return intent.intent_id


def test_a_leftover_standard_intent_cannot_be_actuated_the_next_day(tmp_path, monkeypatch):
    """The reproduced hole: an auto-approved standard change, no evidence, acted on."""
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    chg = _standard_change(itil)
    iid = _proposed_intent(ledger, chg.id, evidence=FRESH)  # observed a day before the clock
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn)
    assert apply_fn.calls == []
    assert outcomes[0].outcome.startswith("refused: evidence stale")
    # Voided, so it cannot come back on a later pass either.
    assert ledger.current_state(iid) is action_ledger.ActionState.ESCALATED
    assert _dispatch_pass(paths, observer, ledger, itil, apply_fn) == []


def test_a_standard_intent_with_no_stored_evidence_is_refused_and_voided(tmp_path, monkeypatch):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _standard_change(itil).id)  # a legacy intent: no stamp
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn, clock=lambda: CLOCK)
    assert apply_fn.calls == []
    assert "evidence missing" in outcomes[0].outcome
    assert ledger.current_state(iid) is action_ledger.ActionState.ESCALATED


def test_a_standard_intent_with_fresh_stored_evidence_and_a_firing_condition_actuates(
    tmp_path, monkeypatch
):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _standard_change(itil).id, evidence=FRESH)
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn, clock=lambda: CLOCK)
    assert outcomes[0].outcome == "verified"
    assert len(apply_fn.calls) == 1
    assert ledger.current_state(iid) is action_ledger.ActionState.VERIFIED


def test_the_dispatcher_re_observes_and_will_not_act_on_a_condition_that_cleared(
    tmp_path, monkeypatch
):
    """Human-approved honor-pending skips the age limit but must re-observe."""
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    observer.healed = True  # it fixed itself while the approval was pending
    iid = _proposed_intent(ledger, _normal_change_approved_by_a_human(itil).id, lane="escalate")
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn)  # a day later
    assert apply_fn.calls == []
    assert "condition cleared before dispatch" in outcomes[0].outcome
    # A human approval covers ONE episode: when it clears, the intent is closed and the
    # change retired, so a recurrence needs a new proposal and a new human vote.
    assert ledger.current_state(iid) is action_ledger.ActionState.ESCALATED


def test_human_approved_intent_with_a_firing_condition_actuates_a_day_later_without_evidence(
    tmp_path, monkeypatch
):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _normal_change_approved_by_a_human(itil).id, lane="escalate")
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn)
    assert outcomes[0].outcome == "verified"
    assert ledger.current_state(iid) is action_ledger.ActionState.VERIFIED


def test_no_owning_observer_means_no_actuation(tmp_path, monkeypatch):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    _proposed_intent(ledger, _normal_change_approved_by_a_human(itil).id, lane="escalate")
    apply_fn = _Apply(observer)
    outcomes = dispatch.run_dispatch_pass(
        paths,
        ledger,
        itil,
        adapters={},
        problem_types=set(),  # Ready is a health type: fires when False
        apply_fn=apply_fn,
        now_iso=NOW,
        clock=lambda: CLOCK,
    )
    assert apply_fn.calls == []
    assert "observer unavailable" in outcomes[0].outcome


def test_ledger_allows_voiding_a_proposed_intent_but_nothing_else_from_proposed(tmp_path):
    ledger = action_ledger.ActionLedger(tmp_path / "l")
    iid = _proposed_intent(ledger, "chg-x")
    with pytest.raises(ValueError):
        ledger.append(
            iid,
            action_ledger.ActionState.VERIFIED,
            occurred_at=CLOCK,
            actor="test",
        )
    ledger.append(
        iid,
        action_ledger.ActionState.ESCALATED,
        occurred_at=CLOCK,
        actor="test",
        detail={"voided": True},
    )
    assert ledger.current_state(iid) is action_ledger.ActionState.ESCALATED


def test_a_standard_intent_with_no_recorded_lane_is_refused_and_never_voided(
    tmp_path, monkeypatch
):
    """A legacy intent cannot be told apart from a pending human decision: never act on
    it, never destroy it."""
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _standard_change(itil).id, evidence=FRESH, lane=None)
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn, clock=lambda: CLOCK)
    assert apply_fn.calls == []
    assert "no recorded auto lane" in outcomes[0].outcome
    assert ledger.current_state(iid) is action_ledger.ActionState.PROPOSED


@pytest.mark.parametrize("status", ["Unknown", None])
def test_unknown_or_unobserved_is_not_firing_and_the_intent_stays(tmp_path, monkeypatch, status):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    observer.status_override = status or "Unknown"
    iid = _proposed_intent(ledger, _normal_change_approved_by_a_human(itil).id, lane="escalate")
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(paths, observer, ledger, itil, apply_fn)
    assert apply_fn.calls == []
    assert "Unknown" in outcomes[0].outcome
    assert ledger.current_state(iid) is action_ledger.ActionState.PROPOSED


def test_an_unknown_standard_intent_is_not_voided_either(tmp_path, monkeypatch):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    observer.status_override = "Unknown"
    iid = _proposed_intent(ledger, _standard_change(itil).id, evidence=FRESH)
    apply_fn = _Apply(observer)
    _dispatch_pass(paths, observer, ledger, itil, apply_fn, clock=lambda: CLOCK)
    assert apply_fn.calls == []
    assert ledger.current_state(iid) is action_ledger.ActionState.PROPOSED


def test_a_re_observation_older_than_the_allowed_age_does_not_act(tmp_path, monkeypatch):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _normal_change_approved_by_a_human(itil).id, lane="escalate")
    apply_fn = _Apply(observer)
    clock = lambda: CLOCK + timedelta(days=1)  # noqa: E731
    observer.stamp = "2026-08-20T00:00:30Z"  # the adapter hands back a day-old reading
    outcomes = dispatch.run_dispatch_pass(
        paths,
        ledger,
        itil,
        adapters={"demo": observer},
        problem_types=set(),
        apply_fn=apply_fn,
        now_iso=NOW,
        clock=clock,
    )
    assert apply_fn.calls == []
    assert "stale" in outcomes[0].outcome
    assert ledger.current_state(iid) is action_ledger.ActionState.PROPOSED


def test_a_naive_clock_is_refused_not_read_as_local_time(tmp_path, monkeypatch):
    paths, observer, ledger, itil = _setup(tmp_path, monkeypatch)
    iid = _proposed_intent(ledger, _standard_change(itil).id, evidence=FRESH)
    apply_fn = _Apply(observer)
    outcomes = _dispatch_pass(
        paths, observer, ledger, itil, apply_fn, clock=lambda: datetime(2026, 8, 20, 0, 0, 30)
    )
    assert apply_fn.calls == []
    assert "timezone" in outcomes[0].outcome
