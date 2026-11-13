"""The dispatcher acts only on an authenticated human approval (card bada5c86).

Before this card the dispatcher inherited three holes from the ITIL fold through
``_is_human_approval``: a bare ``agent="human"`` vote (what the MCP tool writes when
CapAuth resolution fails) counted; a vote by the change's creator or managed-by
owner counted; and ``skoperator decide`` recorded the running agent's own identity
with a self-asserted ``approver`` role. Each case below must leave the intent
PROPOSED with ``apply_fn`` never called, whatever skcoord release is installed.

The intents here are on the human (``escalate``) lane, which the dispatcher judges
from the votes itself (``qualifying_human_approval``). The ``auto`` lane trusts the
fold's change status, so it is closed by the skcoord half of this card; that test
skips on an skcoord release without it.
"""

from __future__ import annotations

import pytest

import skcapstone.itil as itil_module
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import action_ledger, decisions, dispatch

from .test_dispatch import (  # noqa: F401  (autouse fixture re-dates votes)
    _CLOCK,
    _NOW,
    _apply_fn_that_heals,
    _FakeObserver,
    _ledger,
    _make_proposed_intent,
    _paths,
    _propose,
    _provision,
    _votes_decided_before_the_test_clock,
)

FPR = "A" * 40
SIGNED = {"subject_fingerprint": FPR, "authorization_id": "authz-test"}


def _world(tmp_path, *, created_by="atlas", managed_by="atlas"):
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by=created_by,
        managed_by=managed_by,
        tags=["operator"],
    )
    return paths, itil, ledger, chg


def _dispatch(paths, ledger, itil, chg, lane="escalate"):
    observer = _FakeObserver("Ready", "widget-1")
    calls = []

    def apply_fn(prop, classification):
        calls.append(prop)
        return _apply_fn_that_heals(observer)(prop, classification)

    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id, lane=lane)
    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=apply_fn,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    return outcome, calls, ledger.current_state(intent_id)


def _assert_never_actuates(paths, ledger, itil, chg, lane="escalate"):
    outcome, calls, state = _dispatch(paths, ledger, itil, chg, lane)
    assert calls == []
    assert state is action_ledger.ActionState.PROPOSED
    assert outcome.outcome.startswith("pending:")


def test_authenticated_human_approval_actuates(tmp_path):
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(
        chg.id,
        agent="chef",
        decision="approved",
        subject="chef",
        subject_role="owner",
        **SIGNED,
    )
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg)
    assert outcome.outcome == "verified"
    assert len(calls) == 1


def test_subject_less_human_vote_never_actuates(tmp_path):
    """The MCP fallback when CapAuth resolution fails."""
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert not dispatch.qualifying_human_approval(itil.get_cab_votes(chg.id)[0], chg)
    _assert_never_actuates(paths, ledger, itil, chg)


def test_self_asserted_role_without_signed_grant_never_actuates(tmp_path):
    """What `skoperator decide` used to write: the agent's identity plus `approver`."""
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(
        chg.id,
        agent="capauth:lumina@skworld.io",
        decision="approved",
        subject="capauth:lumina@skworld.io",
        subject_role="approver",
    )
    _assert_never_actuates(paths, ledger, itil, chg)


@pytest.mark.parametrize("field", ["created_by", "managed_by"])
def test_creator_or_managed_by_vote_never_actuates(tmp_path, field):
    owners = {"created_by": "atlas", "managed_by": "atlas", field: "chef"}
    paths, itil, ledger, chg = _world(tmp_path, **owners)
    itil.submit_cab_vote(
        chg.id,
        agent="capauth:chef@skworld.io",
        decision="approved",
        subject="capauth:chef@skworld.io",
        subject_role="owner",
        **SIGNED,
    )
    _assert_never_actuates(paths, ledger, itil, chg)


@pytest.mark.parametrize("voter", ["operator", "capauth:operator@skworld.io", "atlas"])
def test_operator_identity_vote_never_actuates(tmp_path, voter):
    paths, itil, ledger, chg = _world(tmp_path, created_by="lumina", managed_by="lumina")
    itil.submit_cab_vote(
        chg.id, agent=voter, decision="approved", subject=voter, subject_role="owner", **SIGNED
    )
    _assert_never_actuates(paths, ledger, itil, chg)


def _park(ddir, chg):
    decisions.park(
        str(ddir),
        [{"app": "widget", "action": "fix-it", "object": "widget-1", "itil_change_id": chg.id}],
        decision_id="d1",
        created_iso=_NOW,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # the running agent's own identity, self-asserted role, no grant
        {"subject_fingerprint": FPR},
        {"authorization_id": "authz-test"},
        {**SIGNED, "subject_role": "operator"},
    ],
)
def test_resolve_decision_refuses_an_approval_without_a_verified_grant(tmp_path, kwargs):
    _, itil, _, chg = _world(tmp_path)
    ddir = tmp_path / "decisions"
    _park(ddir, chg)
    with pytest.raises(dispatch.UnauthorizedDecisionError, match="verified human"):
        dispatch.resolve_decision(
            str(ddir),
            itil,
            "d1",
            approve=True,
            choice=0,
            subject="capauth:lumina@skworld.io",
            resolved_iso=_NOW,
            **kwargs,
        )
    assert itil.get_cab_votes(chg.id) == []
    assert len(decisions.list_pending(str(ddir))) == 1


def test_resolve_decision_refuses_the_operator_identity_even_with_a_grant(tmp_path):
    _, itil, _, chg = _world(tmp_path, created_by="lumina", managed_by="lumina")
    ddir = tmp_path / "decisions"
    _park(ddir, chg)
    with pytest.raises(dispatch.UnauthorizedDecisionError):
        dispatch.resolve_decision(
            str(ddir),
            itil,
            "d1",
            approve=True,
            choice=0,
            subject="capauth:operator@skworld.io",
            resolved_iso=_NOW,
            **SIGNED,
        )
    assert itil.get_cab_votes(chg.id) == []


def test_resolve_decision_with_a_verified_grant_authorizes_dispatch(tmp_path):
    paths, itil, ledger, chg = _world(tmp_path)
    ddir = tmp_path / "decisions"
    _park(ddir, chg)
    dispatch.resolve_decision(
        str(ddir),
        itil,
        "d1",
        approve=True,
        choice=0,
        subject="chef",
        resolved_iso=_NOW,
        subject_role="owner",
        **SIGNED,
    )
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg)
    assert outcome.outcome == "verified"
    assert len(calls) == 1


@pytest.mark.skipif(
    not hasattr(itil_module, "_qualifying_approvals"),
    reason="needs the skcoord release with card bada5c86 (fold no longer approves)",
)
@pytest.mark.parametrize(
    "vote",
    [
        {"agent": "human", "decision": "approved"},
        {"agent": "chef", "decision": "approved", "subject": "chef", "subject_role": "owner"},
    ],
)
def test_auto_lane_does_not_ride_an_unauthenticated_vote(tmp_path, vote):
    """A normal change is not auto-approved; with the fixed fold an unauthenticated
    vote leaves it proposed, so an auto-lane intent bound to it waits."""
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(chg.id, **vote)
    assert next(c for c in itil.list_changes() if c.id == chg.id).status.value == "proposed"
    _assert_never_actuates(paths, ledger, itil, chg, lane="auto")
