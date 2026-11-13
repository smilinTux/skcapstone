"""The dispatcher acts only on an authenticated human approval (card bada5c86).

Before this card the dispatcher inherited three holes from the ITIL fold through
``_is_human_approval``: a bare ``agent="human"`` vote (what the MCP tool writes when
CapAuth resolution fails) counted; a vote by the change's creator or managed-by
owner counted; and ``skoperator decide`` recorded the running agent's own identity
with a self-asserted ``approver`` role. Each case below must leave the intent
PROPOSED with ``apply_fn`` never called, whatever skcoord release is installed.

A qualifying approval stores the signed grant it was cast under, and the grant must
verify against a pinned approver key (``skcapstone.cab_approval``). The tests pin a
throwaway key (``tests/conftest.py``). Both lanes are judged from the votes and the
change's own shape, never from the fold's status alone, so every test here runs on
every skcoord release, including 0.1.90 and 0.1.96, whose fold approves a normal
change on a bare ``human`` vote.
"""

from __future__ import annotations

import pytest

from skcapstone.cab_grants_testing import cast_signed_vote, envelope
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
#: Provenance fields with no grant behind them: what a caller can write by hand.
FORGED = {"subject_fingerprint": FPR, "authorization_id": "authz-test"}


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
    cast_signed_vote(itil, chg.id, subject="chef", role="owner")
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg)
    assert outcome.outcome == "verified"
    assert len(calls) == 1


def test_subject_less_human_vote_never_actuates(tmp_path):
    """The MCP fallback when CapAuth resolution fails."""
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert not dispatch.qualifying_human_approval(itil, itil.get_cab_votes(chg.id)[0], chg)
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


def test_provenance_fields_without_a_stored_grant_never_actuate(tmp_path):
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(
        chg.id, agent="chef", decision="approved", subject="chef", subject_role="owner", **FORGED
    )
    _assert_never_actuates(paths, ledger, itil, chg)


def test_grant_from_an_unpinned_signer_never_actuates(tmp_path):
    paths, itil, ledger, chg = _world(tmp_path)
    cast_signed_vote(itil, chg.id, subject="chef", role="owner", signer="mallory")
    _assert_never_actuates(paths, ledger, itil, chg)


def test_no_pin_file_means_no_human_approval(tmp_path, monkeypatch):
    from skcapstone import cab_grants_testing

    paths, itil, ledger, chg = _world(tmp_path)
    cast_signed_vote(itil, chg.id, subject="chef", role="owner")
    for module, attr in cab_grants_testing.pin_targets():
        monkeypatch.setattr(module, attr, tmp_path / "absent.json")
    _assert_never_actuates(paths, ledger, itil, chg)


def test_a_grant_bound_to_another_change_never_actuates(tmp_path):
    """The stored envelope must name this change: copying a grant across fails."""
    import json

    paths, itil, ledger, chg = _world(tmp_path)
    other = envelope("chg-other", issuer="chef", role="owner")
    itil.submit_cab_vote(
        chg.id,
        agent="chef",
        decision="approved",
        subject="chef",
        subject_role="owner",
        subject_fingerprint=other["issuer_fingerprint"],
        authorization_id=other["authorization_id"],
    )
    path = itil.cab_dir / f"{chg.id}-chef.json"
    data = json.loads(path.read_text())
    data["authorization"] = other
    path.write_text(json.dumps(data))
    _assert_never_actuates(paths, ledger, itil, chg)


@pytest.mark.parametrize("field", ["created_by", "managed_by"])
def test_creator_or_managed_by_vote_never_actuates(tmp_path, field):
    owners = {"created_by": "atlas", "managed_by": "atlas", field: "chef"}
    paths, itil, ledger, chg = _world(tmp_path, **owners)
    cast_signed_vote(itil, chg.id, subject="capauth:chef@skworld.io", role="owner")
    _assert_never_actuates(paths, ledger, itil, chg)


@pytest.mark.parametrize("voter", ["operator", "capauth:operator@skworld.io", "atlas"])
def test_operator_identity_vote_never_actuates(tmp_path, voter):
    paths, itil, ledger, chg = _world(tmp_path, created_by="lumina", managed_by="lumina")
    # Even a grant that verifies (the operator's key pinned by mistake) never counts.
    cast_signed_vote(itil, chg.id, subject=voter, role="owner")
    _assert_never_actuates(paths, ledger, itil, chg)


def _park(ddir, chg):
    decisions.park(
        str(ddir),
        [{"app": "widget", "action": "fix-it", "object": "widget-1", "itil_change_id": chg.id}],
        decision_id="d1",
        created_iso=_NOW,
    )


def _grant(chg, issuer="capauth:lumina@skworld.io", **kw):
    return envelope(chg.id, issuer=issuer, role=kw.pop("role", "approver"), **kw)


@pytest.mark.parametrize(
    "make",
    [
        lambda chg: None,  # the running agent's own identity, no grant
        lambda chg: {**FORGED},  # provenance fields, not an envelope
        lambda chg: _grant(chg, signer="mallory"),  # unpinned signer
        lambda chg: _grant(chg, decision="rejected"),  # grant for another decision
        lambda chg: {**_grant(chg), "change_id": "chg-other"},  # tampered
        lambda chg: envelope("chg-other", issuer="capauth:lumina@skworld.io"),
        lambda chg: _grant(chg, issuer="chef"),  # issuer is not the subject
    ],
    ids=[
        "no-grant",
        "fields-only",
        "unpinned",
        "wrong-decision",
        "tampered",
        "other-change",
        "issuer-mismatch",
    ],
)
def test_resolve_decision_refuses_an_approval_without_a_verified_grant(tmp_path, make):
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
            authorization=make(chg),
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
            authorization=_grant(chg, issuer="capauth:operator@skworld.io"),
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
        authorization=_grant(chg, issuer="chef", role="owner"),
    )
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg)
    assert outcome.outcome == "verified"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "vote",
    [
        {"agent": "human", "decision": "approved"},
        {"agent": "chef", "decision": "approved", "subject": "chef", "subject_role": "owner"},
        {
            "agent": "chef",
            "decision": "approved",
            "subject": "chef",
            "subject_role": "owner",
            **FORGED,
        },
    ],
    ids=["bare-human", "self-asserted-role", "forged-fields"],
)
def test_auto_lane_does_not_ride_an_unauthenticated_vote(tmp_path, vote):
    """Review item 2: the auto lane used to infer auto approval from "approved and
    not human-approved", and skcoord 0.1.96 and 0.1.90 fold a normal change approved
    on these votes. A normal change without the auto-normal shape is never auto
    approved, so the intent waits on every skcoord release."""
    paths, itil, ledger, chg = _world(tmp_path)
    itil.submit_cab_vote(chg.id, **vote)
    _assert_never_actuates(paths, ledger, itil, chg, lane="auto")


def test_auto_lane_still_actuates_an_auto_normal_change(tmp_path):
    """The positive derivation keeps the real auto-normal path working."""
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
        created_by="operator",
        managed_by="atlas",
        tags=["operator", "auto-normal"],
    )
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg, lane="auto")
    assert outcome.outcome == "verified"
    assert len(calls) == 1
