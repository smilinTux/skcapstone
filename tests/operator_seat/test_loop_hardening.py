"""ATLAS gate 5 hardening: the three weak spots the drill documented.

1. stale or missing evidence must never actuate (fail closed),
2. a freeze landing mid-pass is reported at the decision point,
3. the outcome says "verified" only after verification ran and passed.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skcapstone.fleet import store
from skcapstone.operator_seat import action_ledger, loop

from .test_loop import _enroll

NOW = "2026-08-20T00:00:00Z"
CLOCK = datetime(2026, 8, 20, 0, 0, 30, tzinfo=timezone.utc)  # the real clock, injected
FRESH = "2026-08-19T23:59:30Z"
HUMAN = store.Writer(role="operator", node="node-158", identity="chef")


def _stamp(observed_at=FRESH, source="obs:demo:svc"):
    return {"source": source, "observed_at": observed_at}


def _prop(**extra):
    return {"action": "restart_service", "object": "web", **extra}


def _provision(paths):
    store.set_frozen(paths, False, writer=HUMAN, reason="test provisioning")


def _run(paths, proposals, *, apply_fn, **kwargs):
    _provision(paths)
    kwargs.setdefault("clock", lambda: CLOCK)
    return loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=lambda b, r: proposals,
        apply_fn=apply_fn,
        execute=True,
        emit=lambda _s: None,
        **kwargs,
    )


class _Recorder:
    def __init__(self, result=None):
        self.calls = []
        self.result = {"performed": True} if result is None else result

    def __call__(self, prop, classification):
        self.calls.append(prop)
        return self.result


# --------------------------------------------------------------------------
# 1. stale evidence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra, expected",
    [
        ({"evidence": _stamp("2019-01-01T00:00:00Z")}, "stale"),
        ({"evidence": _stamp("2026-08-19T23:49:00Z")}, "stale"),  # 11 min, max is 10
        ({}, "missing"),
        ({"evidence_ref": f"obs:{FRESH}:free-text"}, "missing"),
        ({"evidence": _stamp("not-a-time")}, "unparseable"),
        ({"evidence": _stamp("2026-08-19T23:59:30")}, "unparseable"),
        ({"evidence": _stamp("2026-08-20T01:00:00Z")}, "future"),
    ],
)
def test_actuation_is_refused_without_fresh_structured_evidence(
    tmp_path, monkeypatch, extra, expected
):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    apply_fn = _Recorder()
    res = _run(paths, [_prop(**extra)], apply_fn=apply_fn)
    assert apply_fn.calls == []
    outcome = res["outcomes"][0]["outcome"]
    assert outcome.startswith("refused: ")
    assert expected in outcome


def test_refused_evidence_creates_no_ledger_intent_and_burns_no_retry_budget(
    tmp_path, monkeypatch
):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)
    apply_fn = _Recorder()
    res = _run(
        paths,
        [_prop(evidence=_stamp("2019-01-01T00:00:00Z"))],
        apply_fn=apply_fn,
        lifecycle_ledger=ledger,
        execution_state=state,
    )
    assert apply_fn.calls == []
    assert res["outcomes"][0]["intent_id"] is None
    assert ledger.list_intents() == []
    assert state._load()["actions"] == {}


def test_fresh_evidence_actuates_and_the_ledger_records_its_timestamp(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    apply_fn = _Recorder()
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    human = HUMAN
    store.write_spec(
        paths,
        "operatorapp",
        "demo",
        {
            "name": "demo",
            "conditions": ["Ready"],
            "proposedStandardActions": ["restart"],
            "ratifiedStandardActions": ["restart"],
        },
        writer=human,
    )
    seen = {"n": 0}

    def observe(_p, _n):
        seen["n"] += 1
        status = "False" if seen["n"] == 1 else "True"
        return {"conditions": [{"type": "Ready", "status": status, "object": "svc"}]}

    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observe})
    prop = {
        "app": "demo",
        "condition": "Ready",
        "action": "restart",
        "object": "svc",
        "evidence": _stamp(),
    }
    res = loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=lambda b, r: [prop],
        explain={"actions": [{"name": "restart", "standard": True, "reversible": True}]},
        apply_fn=apply_fn,
        execute=True,
        lifecycle_ledger=ledger,
        emit=lambda _s: None,
    )
    assert apply_fn.calls == [prop]
    intent_id = res["outcomes"][0]["intent_id"]
    assert ledger.events(intent_id)[0].evidence_ref == f"obs:demo:svc@{FRESH}"


def test_max_evidence_age_is_configurable(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    two_minutes_old = _stamp("2026-08-19T23:58:00Z")
    tight = _Recorder()
    res = _run(paths, [_prop(evidence=two_minutes_old)], apply_fn=tight, max_evidence_age_s=60)
    assert tight.calls == [] and "stale" in res["outcomes"][0]["outcome"]
    loose = _Recorder()
    _run(paths, [_prop(evidence=two_minutes_old)], apply_fn=loose, max_evidence_age_s=3600)
    assert len(loose.calls) == 1


@pytest.mark.parametrize("bad", [0, -5, float("nan"), None, True])
def test_a_bad_max_evidence_age_refuses_rather_than_disabling_the_check(
    tmp_path, monkeypatch, bad
):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    apply_fn = _Recorder()
    res = _run(paths, [_prop(evidence=_stamp())], apply_fn=apply_fn, max_evidence_age_s=bad)
    assert apply_fn.calls == []
    assert res["outcomes"][0]["outcome"].startswith("refused: ")


def test_evidence_is_not_required_when_execution_is_off(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    res = loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=lambda b, r: [_prop()],
        apply_fn=_Recorder(),
        execute=False,
        emit=lambda _s: None,
    )
    assert res["outcomes"][0]["outcome"].startswith("auto-ready")


# --------------------------------------------------------------------------
# 2. freeze reported at the decision point
# --------------------------------------------------------------------------


def test_pass_with_no_freeze_reports_not_frozen_at_the_decision(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    _provision(paths)
    res = _run(paths, [_prop(evidence=_stamp())], apply_fn=_Recorder())
    assert res["frozen"] is False
    assert res["frozen_mid_pass"] is False
    assert res["freeze_state"] == "active"
    assert res["outcomes"][0]["freeze_state"] == "active"


def test_freeze_landing_mid_pass_is_reported_and_refuses_the_action(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    _provision(paths)
    apply_fn = _Recorder()
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)

    def propose_then_freeze(brief, route):
        store.set_frozen(paths, True, writer=HUMAN, reason="mid-pass kill switch")
        return [_prop(evidence=_stamp())]

    res = loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=propose_then_freeze,
        apply_fn=apply_fn,
        execute=True,
        execution_state=state,
        emit=lambda _s: None,
    )
    assert apply_fn.calls == []
    assert res["frozen"] is True
    assert res["frozen_mid_pass"] is True
    assert res["freeze_state"] == "frozen"
    outcome = res["outcomes"][0]
    assert outcome["freeze_state"] == "frozen"
    assert outcome["outcome"].startswith("refused: ")
    assert "frozen" in outcome["outcome"]
    # A kill switch is not a fault: it must not feed the circuit breaker.
    assert state._load()["actions"] == {}


def test_freeze_landing_during_actuation_is_reported_and_not_counted_as_a_failure(
    tmp_path, monkeypatch
):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    _provision(paths)
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)

    def freeze_then_refuse(prop, classification):
        store.set_frozen(paths, True, writer=HUMAN, reason="lands inside the actuator")
        raise RuntimeError("fleet is frozen: the operator does not actuate")

    res = _run(
        paths,
        [_prop(evidence=_stamp())],
        apply_fn=freeze_then_refuse,
        execution_state=state,
    )
    assert res["frozen"] is True
    assert res["frozen_mid_pass"] is True
    assert res["freeze_state"] == "frozen"
    assert res["outcomes"][0]["freeze_state"] == "frozen"
    assert state._load()["actions"] == {}


def test_a_pass_that_starts_frozen_is_not_flagged_mid_pass(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    store.set_frozen(paths, True, writer=HUMAN, reason="standing")
    res = loop.run_once(paths, now_iso=NOW, emit=lambda _s: None)
    assert res["frozen"] is True
    assert res["frozen_mid_pass"] is False


# --------------------------------------------------------------------------
# 3. "applied" before verification
# --------------------------------------------------------------------------


def _demo_app(paths, monkeypatch):
    store.write_spec(
        paths,
        "operatorapp",
        "demo",
        {
            "name": "demo",
            "conditions": ["Ready"],
            "proposedStandardActions": ["restart"],
            "ratifiedStandardActions": ["restart"],
        },
        writer=HUMAN,
    )
    seen = {"n": 0}

    def observe(_p, _n):
        seen["n"] += 1
        status = "False" if seen["n"] == 1 else "True"
        return {"conditions": [{"type": "Ready", "status": status, "object": "svc"}]}

    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observe})
    return {
        "app": "demo",
        "condition": "Ready",
        "action": "restart",
        "object": "svc",
        "evidence": _stamp(),
    }


_EXPLAIN = {"actions": [{"name": "restart", "standard": True, "reversible": True}]}


def test_no_verification_means_performed_unverified_never_applied(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    res = _run(paths, [_prop(evidence=_stamp())], apply_fn=_Recorder())
    assert res["outcomes"][0]["outcome"] == "performed, unverified"


def test_ledger_alone_runs_verification_so_a_pass_reads_verified(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    prop = _demo_app(paths, monkeypatch)
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    res = loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=lambda b, r: [prop],
        explain=_EXPLAIN,
        apply_fn=_Recorder(),
        execute=True,
        lifecycle_ledger=ledger,
        emit=lambda _s: None,
    )
    outcome = res["outcomes"][0]
    assert outcome["outcome"] == "verified"
    assert ledger.current_state(outcome["intent_id"]) is action_ledger.ActionState.VERIFIED


def test_a_failed_verification_is_never_reported_as_applied_or_verified(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    prop = _demo_app(paths, monkeypatch)
    # Condition never clears after the action.
    monkeypatch.setattr(
        loop,
        "ADAPTERS",
        {
            "demo": lambda _p, _n: {
                "conditions": [{"type": "Ready", "status": "False", "object": "svc"}]
            }
        },
    )
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    res = loop.run_once(
        paths,
        now_iso=NOW,
        clock=lambda: CLOCK,
        propose=lambda b, r: [prop],
        explain=_EXPLAIN,
        apply_fn=_Recorder(),
        execute=True,
        lifecycle_ledger=ledger,
        emit=lambda _s: None,
    )
    outcome = res["outcomes"][0]["outcome"]
    assert outcome.startswith("failed:")
    assert "applied" not in outcome and not outcome.startswith("verified")


def test_a_moving_evidence_timestamp_cannot_dodge_the_cooldown(tmp_path, monkeypatch):
    """Evidence is not part of the action fingerprint: each pass restamps it."""
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision(paths)
    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=900)
    first = _Recorder({"performed": False, "reason": "boom"})
    _run(
        paths,
        [_prop(evidence=_stamp("2026-08-19T23:59:00Z"))],
        apply_fn=first,
        execution_state=state,
    )
    second = _Recorder()
    res = _run(
        paths,
        [_prop(evidence=_stamp("2026-08-19T23:59:55Z"))],
        apply_fn=second,
        execution_state=state,
    )
    assert len(first.calls) == 1
    assert second.calls == []
    assert "cooldown" in res["outcomes"][0]["outcome"]
