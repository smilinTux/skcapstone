"""Tests for the trustee lifecycle actuation gate (card e51a3e7e).

Covers trustee_actuation.py directly: authorize()'s fail-closed shape
(mirroring fleet.operator_http.authorize's own test coverage), the
change_is_approved() narrow ITIL check, and guard()'s composition and
ordering. TrusteeOps-level integration (the gate wired into
restart_agent/scale_agent/rotate_agent) is covered by
tests/test_trustee_ops.py::TestActuationGate.
"""

from __future__ import annotations

import sys
from pathlib import Path

from skcapstone import trustee_actuation as ta
from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths


def _writer() -> store.Writer:
    return store.Writer(role="operator", node="test-node", identity="test")


def _provisioned_open(tmp_path: Path) -> FleetPaths:
    paths = FleetPaths(root=tmp_path / "fleet")
    store.set_frozen(paths, False, writer=_writer(), reason="test provisioning")
    return paths


class _FakeDecision:
    def __init__(self, allow: bool, reason: str) -> None:
        self.allow = allow
        self.reason = reason


# ---------------------------------------------------------------------------
# authorize() -- mirrors fleet/test_operator_http.py's own coverage of the
# pattern this module follows.
# ---------------------------------------------------------------------------


def test_authorize_allow_and_deny():
    def decide_allow(subject, capability, **kw):
        return _FakeDecision(True, "ok")

    def decide_deny(subject, capability, **kw):
        return _FakeDecision(False, "no grant")

    assert ta.authorize("fp", ta.CAP_RESTART, decide_fn=decide_allow) == (True, "ok")
    assert ta.authorize("fp", ta.CAP_RESTART, decide_fn=decide_deny) == (False, "no grant")


def test_authorize_fails_closed_with_a_real_empty_capauth_store(tmp_path):
    """No capability tokens anywhere: decide() denies, never raises."""
    from capauth import decide as real_decide

    allow, reason = ta.authorize(
        "deadbeef", ta.CAP_RESTART, decide_fn=real_decide, base_dir=tmp_path / "empty"
    )
    assert allow is False
    assert reason


def test_authorize_fails_closed_when_capauth_is_unreachable(monkeypatch):
    """The negative test the coord card calls out explicitly: an
    unreachable capauth PDP must deny, never raise past the caller and
    never silently allow. No decide_fn override, so the real
    `from capauth import decide` import is what fails."""
    monkeypatch.setitem(sys.modules, "capauth", None)
    allow, reason = ta.authorize("fp", ta.CAP_RESTART)
    assert allow is False
    assert "capauth unavailable" in reason


def test_authorize_capability_scoping_is_independent_per_verb():
    """A subject granted ONLY trustee.restart must be denied trustee.rotate."""

    def decide_fn(subject, capability, **kw):
        return _FakeDecision(capability == ta.CAP_RESTART, "scoped grant")

    assert ta.authorize("fp", ta.CAP_RESTART, decide_fn=decide_fn)[0] is True
    assert ta.authorize("fp", ta.CAP_ROTATE, decide_fn=decide_fn)[0] is False


def test_trustee_rules_cover_all_three_verbs_at_verified():
    from capauth.pairing import EnrollmentMode

    rules = ta._trustee_rules()
    assert set(rules) == {ta.CAP_RESTART, ta.CAP_SCALE, ta.CAP_ROTATE}
    assert all(rule.minimum_mode == EnrollmentMode.VERIFIED for rule in rules.values())


# ---------------------------------------------------------------------------
# resolve_subject()
# ---------------------------------------------------------------------------


def test_resolve_subject_never_raises_when_capauth_is_unreachable(monkeypatch):
    monkeypatch.setitem(sys.modules, "capauth", None)
    assert ta.resolve_subject() is None


# ---------------------------------------------------------------------------
# change_is_approved()
# ---------------------------------------------------------------------------


def test_change_is_approved_true_only_after_cab_approval(tmp_path):
    from skcapstone.itil import ITILManager

    mgr = ITILManager(tmp_path)
    change = mgr.propose_change(title="rotate atlas", managed_by="atlas")

    assert ta.change_is_approved(change.id, shared_root=tmp_path) is False

    mgr.submit_cab_vote(
        change.id,
        agent="chef",
        decision="approved",
        subject="chef",
        subject_role="owner",
        subject_fingerprint="A" * 40,
        authorization_id="authz-test",
    )
    assert ta.change_is_approved(change.id, shared_root=tmp_path) is True


def test_change_is_approved_false_for_unknown_or_empty_id(tmp_path):
    assert ta.change_is_approved("chg-doesnotexist", shared_root=tmp_path) is False
    assert ta.change_is_approved("", shared_root=tmp_path) is False


# ---------------------------------------------------------------------------
# guard() -- composition and ordering (freeze wins first, per section 3.6)
# ---------------------------------------------------------------------------


def _allow_fn(subject, capability, **kw):
    return _FakeDecision(True, "ok")


def test_guard_refuses_unprovisioned(tmp_path):
    paths = FleetPaths(root=tmp_path / "fleet")
    result = ta.guard(
        ta.CAP_RESTART, paths=paths, subject="fp", shared_root=tmp_path, decide_fn=_allow_fn
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_UNPROVISIONED == store.REASON_UNPROVISIONED


def test_guard_refuses_frozen_even_with_a_grant(tmp_path):
    paths = _provisioned_open(tmp_path)
    store.set_frozen(paths, True, writer=_writer(), reason="drill")
    result = ta.guard(
        ta.CAP_RESTART, paths=paths, subject="fp", shared_root=tmp_path, decide_fn=_allow_fn
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_FROZEN == store.REASON_FROZEN


def test_guard_allows_when_ready_and_authorized(tmp_path):
    paths = _provisioned_open(tmp_path)
    result = ta.guard(
        ta.CAP_RESTART, paths=paths, subject="fp", shared_root=tmp_path, decide_fn=_allow_fn
    )
    assert result.allowed is True
    assert result.reason is None


def test_guard_refuses_when_no_subject_resolved(tmp_path):
    """A None subject (identity could not be resolved) denies before ever
    calling decide() -- there is nothing to authorize."""
    paths = _provisioned_open(tmp_path)
    calls = []

    def decide_fn(subject, capability, **kw):
        calls.append(subject)
        return _FakeDecision(True, "ok")

    result = ta.guard(
        ta.CAP_RESTART, paths=paths, subject=None, shared_root=tmp_path, decide_fn=decide_fn
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_CAPABILITY_DENIED
    assert calls == []  # never reached the PDP


def test_guard_refuses_when_capauth_denies(tmp_path):
    paths = _provisioned_open(tmp_path)

    def deny_fn(subject, capability, **kw):
        return _FakeDecision(False, "no grant")

    result = ta.guard(
        ta.CAP_RESTART, paths=paths, subject="fp", shared_root=tmp_path, decide_fn=deny_fn
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_CAPABILITY_DENIED


def test_guard_refuses_when_capauth_unreachable_never_allows(tmp_path, monkeypatch):
    """Same explicit negative test as authorize()'s, exercised through the
    full guard() composition: ready and unfrozen is not enough."""
    paths = _provisioned_open(tmp_path)
    monkeypatch.setitem(sys.modules, "capauth", None)
    result = ta.guard(ta.CAP_RESTART, paths=paths, subject="fp", shared_root=tmp_path)
    assert result.allowed is False
    assert result.reason == ta.REASON_CAPABILITY_DENIED


def test_guard_rotate_requires_change_id_even_when_ready_and_authorized(tmp_path):
    paths = _provisioned_open(tmp_path)
    result = ta.guard(
        ta.CAP_ROTATE,
        paths=paths,
        subject="fp",
        shared_root=tmp_path,
        decide_fn=_allow_fn,
        require_approved_change=True,
        change_id=None,
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_CHANGE_NOT_APPROVED


def test_guard_rotate_refuses_unapproved_change(tmp_path):
    from skcapstone.itil import ITILManager

    mgr = ITILManager(tmp_path)
    change = mgr.propose_change(title="never approved", managed_by="atlas")
    paths = _provisioned_open(tmp_path)
    result = ta.guard(
        ta.CAP_ROTATE,
        paths=paths,
        subject="fp",
        shared_root=tmp_path,
        decide_fn=_allow_fn,
        require_approved_change=True,
        change_id=change.id,
    )
    assert result.allowed is False
    assert result.reason == ta.REASON_CHANGE_NOT_APPROVED


def test_guard_rotate_allows_with_an_approved_change(tmp_path):
    from skcapstone.itil import ITILManager

    mgr = ITILManager(tmp_path)
    change = mgr.propose_change(title="rotate lumina", managed_by="atlas")
    mgr.submit_cab_vote(
        change.id,
        agent="chef",
        decision="approved",
        subject="chef",
        subject_role="owner",
        subject_fingerprint="A" * 40,
        authorization_id="authz-test",
    )
    paths = _provisioned_open(tmp_path)
    result = ta.guard(
        ta.CAP_ROTATE,
        paths=paths,
        subject="fp",
        shared_root=tmp_path,
        decide_fn=_allow_fn,
        require_approved_change=True,
        change_id=change.id,
    )
    assert result.allowed is True


def test_actuation_refused_carries_reason():
    exc = ta.ActuationRefusedError(ta.REASON_FROZEN)
    assert exc.reason == "frozen"
    assert str(exc) == "frozen"

    exc2 = ta.ActuationRefusedError(ta.REASON_CAPABILITY_DENIED, "no grant")
    assert exc2.reason == "capability_denied"
    assert str(exc2) == "no grant"
