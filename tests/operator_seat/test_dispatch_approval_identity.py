"""Which votes count as an authenticated human approval (card bada5c86), and that the
ATLAS dispatcher never actuates on one (card f8dd9441).

Before this card the dispatcher inherited three holes from the ITIL fold through
``_is_human_approval``: a bare ``agent="human"`` vote (what the MCP tool writes when
CapAuth resolution fails) counted; a vote by the change's creator or managed-by
owner counted; and ``skoperator decide`` recorded the running agent's own identity
with a self-asserted ``approver`` role. Each case below must leave the intent
PROPOSED with ``apply_fn`` never called, whatever skcoord release is installed.

Since card f8dd9441 (fewest gates) the ATLAS dispatcher never actuates on a human vote at
all; ``human_approval_times`` still decides what counts for the CMDB apply gate and the
trustee guard, which share it, so the qualification tests below stay.

A qualifying approval stores the signed grant it was cast under, and the grant must
verify against a pinned approver key (``skcapstone.cab_approval``). The tests pin a
throwaway key (``tests/conftest.py``). Both lanes are judged from the votes and the
change's own shape, never from the fold's status alone, so every test here runs on
every skcoord release, including 0.1.90 and 0.1.96, whose fold approves a normal
change on a bare ``human`` vote.
"""

from __future__ import annotations

import pytest

from skcapstone.itil import ITILManager
from skcapstone.operator_seat import action_ledger, decisions, dispatch
from tests.cab_grants import cast_signed_vote, envelope

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
    assert outcome.outcome.startswith(("pending:", dispatch.REPORT_ONLY_REFUSAL))


@pytest.mark.parametrize("lane", ["escalate", "auto"])
def test_an_authenticated_human_approval_qualifies_but_never_actuates_atlas(tmp_path, lane):
    """The vote is a qualifying human approval (it would gate the CMDB apply), yet the
    ATLAS dispatcher refuses it as report-only, before anything is spent."""
    paths, itil, ledger, chg = _world(tmp_path)
    cast_signed_vote(itil, chg.id, subject="chef", role="owner")
    assert dispatch.human_approval_times(itil, chg)
    outcome, calls, state = _dispatch(paths, ledger, itil, chg, lane=lane)
    assert calls == []
    assert outcome.outcome.startswith(dispatch.REPORT_ONLY_REFUSAL)
    assert state is action_ledger.ActionState.PROPOSED


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
    from tests import cab_grants as cab_grants_testing

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


@pytest.mark.parametrize("issuer", ["chef", "capauth:lumina@skworld.io"])
def test_resolve_decision_refuses_an_approval_even_with_a_verified_grant(tmp_path, issuer):
    """No ATLAS decision can be approved (card f8dd9441): refused before any write, so no
    vote is recorded, whatever grant the caller holds."""
    _, itil, _, chg = _world(tmp_path)
    ddir = tmp_path / "decisions"
    _park(ddir, chg)
    assert _grant(chg, issuer=issuer)  # a grant exists; resolve_decision takes none
    with pytest.raises(dispatch.ReportOnlyDecisionError, match="skoperator apps ratify"):
        dispatch.resolve_decision(
            str(ddir),
            itil,
            "d1",
            approve=True,
            choice=0,
            subject=issuer,
            resolved_iso=_NOW,
        )
    assert itil.get_cab_votes(chg.id) == []
    assert len(decisions.list_pending(str(ddir))) == 1


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


def test_approval_age_comes_from_the_signed_grant_not_decided_at(tmp_path, monkeypatch):
    """Round 2: re-dating a vote file's decided_at cannot refresh a stale grant. The
    autouse fixture re-dates every vote to just before the clock; the grant here was
    issued thirty days earlier, so the approval is too old."""
    from datetime import datetime, timedelta

    from tests import cab_grants

    paths, itil, ledger, chg = _world(tmp_path)
    monkeypatch.setattr(
        cab_grants,
        "ISSUED_AT",
        datetime.fromisoformat(_NOW.replace("Z", "+00:00")) - timedelta(days=30),
    )
    cast_signed_vote(itil, chg.id, subject="chef", role="owner")
    (issued,) = dispatch.human_approval_times(itil, chg)
    assert issued.startswith(
        (datetime.fromisoformat(_NOW.replace("Z", "+00:00")) - timedelta(days=30))
        .date()
        .isoformat()
    )
    outcome, calls, _ = _dispatch(paths, ledger, itil, chg)
    assert calls == []
    assert outcome.outcome != "verified"


# ── a vote counts only for the change it was cast on (round 3) ──────────


def _second_change(itil):
    return _propose(
        itil,
        title="fix widget-1 again",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        managed_by="atlas",
        tags=["operator"],
    )


def test_signed_vote_copied_under_another_change_never_actuates_it(tmp_path):
    """The replay: A-chef.json copied byte for byte to B-chef.json."""
    import shutil

    paths, itil, ledger, a = _world(tmp_path)
    b = _second_change(itil)
    cast_signed_vote(itil, a.id, subject="chef", role="owner")
    shutil.copyfile(itil.cab_dir / f"{a.id}-chef.json", itil.cab_dir / f"{b.id}-chef.json")
    assert dispatch.human_approval_times(itil, b) == []
    _assert_never_actuates(paths, ledger, itil, b)


def test_vote_file_renamed_to_another_voter_never_actuates(tmp_path):
    paths, itil, ledger, chg = _world(tmp_path)
    cast_signed_vote(itil, chg.id, subject="chef", role="owner")
    (itil.cab_dir / f"{chg.id}-chef.json").rename(itil.cab_dir / f"{chg.id}-casey.json")
    _assert_never_actuates(paths, ledger, itil, chg)


# ── a signed decision cannot be rolled back (round 4) ───────────────────


def test_replayed_old_approval_after_a_signed_rejection_never_actuates(tmp_path, monkeypatch):
    from datetime import datetime as _dt

    from tests import cab_grants

    paths, itil, ledger, chg = _world(tmp_path)
    monkeypatch.setattr(cab_grants, "ISSUED_AT", _dt.fromisoformat("2026-08-24T22:00:00+00:00"))
    old = envelope(chg.id, issuer="chef", role="owner")
    from skcapstone.cab_approval import record_signed_vote

    record_signed_vote(itil, old)
    monkeypatch.setattr(cab_grants, "ISSUED_AT", _dt.fromisoformat("2026-08-24T23:00:00+00:00"))
    cast_signed_vote(itil, chg.id, subject="chef", role="owner", decision="rejected")
    with pytest.raises(ValueError, match="refusing to replace"):
        record_signed_vote(itil, old)
    assert dispatch.human_approval_times(itil, chg) == []
    outcome, calls, _state = _dispatch(paths, ledger, itil, chg)
    assert calls == []
    assert not outcome.outcome.startswith("verified")
