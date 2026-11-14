"""A CAB vote counts only for the change and voter it was cast for (card bada5c86, round 3).

The replay: a vote file copied byte for byte under another change's name
(``A-chef.json`` to ``B-chef.json``) counted for B. skcoord loaded vote files by
name and never checked the change id inside, and the grant was compared with the
change id from the same copied file. Every gate now binds the change being gated
(from the caller) and the voter named by the file. These run on every skcoord
release; the installed 0.1.96 still loads the copied file.

Also: the CMDB apply gate and the trustee guard checked no approval age, so a
copied or old grant worked there forever. Both now apply the dispatcher's default
from the grant's signed ``issued_at``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone import cab_approval, trustee_actuation
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import cmdb_adapter
from tests import cab_grants
from tests.cab_grants import cast_signed_vote
from tests.operator_seat.test_cmdb_adapter import _CleanManager, _three_artifacts


def _changes(itil, **kw):
    kw.setdefault("change_type", "normal")
    kw.setdefault("risk", "low")
    kw.setdefault("rollback_plan", "revert")
    kw.setdefault("created_by", "atlas")
    kw.setdefault("managed_by", "atlas")
    return itil.propose_change(title="A", **kw), itil.propose_change(title="B", **kw)


def _fold(itil, change_id):
    return next(c for c in itil.list_changes() if c.id == change_id)


def _copy(itil, src_id, dst_id, voter="chef", dst_voter=None):
    shutil.copyfile(
        itil.cab_dir / f"{src_id}-{voter}.json",
        itil.cab_dir / f"{dst_id}-{dst_voter or voter}.json",
    )


def _gate(tmp_path, change_id):
    _three_artifacts(tmp_path)
    return cmdb_adapter._apply_gate(
        tmp_path,
        change_id,
        itil_factory=lambda _home: ITILManager(tmp_path),
        manager_factory=_CleanManager,
    )


@pytest.fixture
def copied(tmp_path):
    """Change A carries a signed Chef approval; its file is copied to change B."""
    itil = ITILManager(tmp_path)
    a, b = _changes(itil)
    cast_signed_vote(itil, a.id, subject="chef", role="owner")
    _copy(itil, a.id, b.id)
    return itil, a, b


def test_copied_signed_vote_is_not_a_vote_for_the_other_change(copied):
    itil, a, b = copied
    assert [v.agent for v in cab_approval.votes_for(itil, a.id)] == ["chef"]
    assert cab_approval.votes_for(itil, b.id) == []
    assert cab_approval.approval_basis(itil, _fold(itil, a.id)) == "human"
    assert cab_approval.approval_basis(itil, _fold(itil, b.id)) is None


def test_copied_signed_vote_does_not_pass_the_cmdb_gate(copied, tmp_path):
    _itil, a, b = copied
    assert _gate(tmp_path, a.id) is None
    assert _gate(tmp_path, b.id) is not None


def test_copied_signed_vote_does_not_pass_the_trustee_guard(copied, tmp_path):
    _itil, a, b = copied
    assert trustee_actuation.change_is_approved(a.id, shared_root=tmp_path) is True
    assert trustee_actuation.change_is_approved(b.id, shared_root=tmp_path) is False


def test_copied_vote_with_its_change_id_rewritten_fails_the_grant(copied):
    itil, _a, b = copied
    path = itil.cab_dir / f"{b.id}-chef.json"
    data = json.loads(path.read_text())
    data["change_id"] = b.id
    path.write_text(json.dumps(data))
    assert [v.change_id for v in cab_approval.votes_for(itil, b.id)] == [b.id]
    assert cab_approval.human_approval_times(itil, _fold(itil, b.id)) == []


def test_filename_voter_must_be_the_vote_agent(tmp_path):
    itil = ITILManager(tmp_path)
    a, _b = _changes(itil)
    cast_signed_vote(itil, a.id, subject="chef", role="owner")
    (itil.cab_dir / f"{a.id}-chef.json").rename(itil.cab_dir / f"{a.id}-casey.json")
    assert cab_approval.votes_for(itil, a.id) == []
    assert cab_approval.human_approval_times(itil, _fold(itil, a.id)) == []
    assert _gate(tmp_path, a.id) is not None


def test_copied_legacy_file_is_not_a_vote_for_the_other_change(tmp_path):
    itil = ITILManager(tmp_path)
    a, b = _changes(itil)
    itil.ensure_dirs()
    payload = json.dumps({"change_id": a.id, "agent": "lumina", "decision": "approved"})
    (itil.cab_dir / f"{a.id}-lumina.json").write_text(payload)
    _copy(itil, a.id, b.id, "lumina")
    assert hashlib.sha256((itil.cab_dir / f"{b.id}-lumina.json").read_bytes()).hexdigest()
    assert cab_approval.votes_for(itil, b.id) == []
    assert cab_approval.approval_basis(itil, _fold(itil, b.id)) is None


def test_votes_for_needs_a_change_id(copied):
    itil, _a, _b = copied
    assert cab_approval.votes_for(itil, "") == []


def test_grant_verification_binds_the_caller_change_id(copied):
    itil, a, b = copied
    vote = cab_approval.votes_for(itil, a.id)[0]
    env = cab_approval.stored_envelope(itil, vote)
    assert cab_approval.verify_vote_envelope(vote, env, change_id=a.id)
    assert not cab_approval.verify_vote_envelope(vote, env, change_id=b.id)
    assert cab_approval.verify_grant_for(env, change_id=a.id, decision="approved")
    assert not cab_approval.verify_grant_for(env, change_id=b.id, decision="approved")


# ── approval age at the CMDB gate and the trustee guard ────────────────


def _aged_vote(tmp_path, monkeypatch, age):
    monkeypatch.setattr(cab_grants, "ISSUED_AT", datetime.now(timezone.utc) - age)
    itil = ITILManager(tmp_path)
    a, _b = _changes(itil)
    cast_signed_vote(itil, a.id, subject="chef", role="owner")
    return a


def test_cmdb_gate_refuses_an_expired_approval(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, timedelta(days=2))
    reason = _gate(tmp_path, a.id)
    assert reason is not None and "expired" in reason


def test_cmdb_gate_accepts_an_approval_within_the_default_age(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, timedelta(hours=23))
    assert _gate(tmp_path, a.id) is None


def test_cmdb_gate_refuses_a_future_dated_approval(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, -timedelta(hours=1))
    reason = _gate(tmp_path, a.id)
    assert reason is not None and "future" in reason


def test_trustee_guard_refuses_an_expired_approval(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, timedelta(days=2))
    assert trustee_actuation.change_is_approved(a.id, shared_root=tmp_path) is False


def test_trustee_guard_accepts_an_approval_within_the_default_age(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, timedelta(hours=23))
    assert trustee_actuation.change_is_approved(a.id, shared_root=tmp_path) is True


def test_trustee_guard_refuses_a_future_dated_approval(tmp_path, monkeypatch):
    a = _aged_vote(tmp_path, monkeypatch, -timedelta(hours=1))
    assert trustee_actuation.change_is_approved(a.id, shared_root=tmp_path) is False


def test_every_gate_shares_one_default_age():
    from skcapstone.operator_seat import dispatch

    assert dispatch.DEFAULT_MAX_APPROVAL_AGE_SECONDS == 86400.0
    assert cab_approval.DEFAULT_MAX_APPROVAL_AGE_SECONDS == 86400.0
