"""A signed CAB decision cannot be rolled back by replaying an older grant (card bada5c86).

Round 4 review: Chef approves change K with a signed grant, then rejects it with a
signed rejection. Any process could then call ``record_signed_vote(itil, old)`` (or
skcoord's ``submit_cab_vote(..., authorization=old)``) and K read approved again,
because the guard only stopped an unsigned vote replacing a signed one. Now a
voter's signed vote is replaced only by a grant whose signed ``issued_at`` is
strictly later, and every gate counts, per voter, only the signed vote with the
latest ``issued_at``. These run on every skcoord release.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone import cab_approval, trustee_actuation
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import cmdb_adapter
from tests import cab_grants
from tests.cab_grants import cast_signed_vote, envelope
from tests.operator_seat.test_cmdb_adapter import _CleanManager, _three_artifacts


def _fold(itil, change_id):
    return next(c for c in itil.list_changes() if c.id == change_id)


def _at(monkeypatch, hours_ago):
    monkeypatch.setattr(
        cab_grants, "ISSUED_AT", datetime.now(timezone.utc) - timedelta(hours=hours_ago)
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
def rejected_after_approval(tmp_path, monkeypatch):
    """K: a signed approval two hours ago, then a signed rejection an hour ago."""
    itil = ITILManager(tmp_path)
    k = itil.propose_change(
        title="K",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        managed_by="atlas",
    )
    _at(monkeypatch, 2)
    old = envelope(k.id, issuer="chef", role="owner")
    cab_approval.record_signed_vote(itil, old)
    assert cab_approval.approval_basis(itil, _fold(itil, k.id)) == "human"
    _at(monkeypatch, 1)
    cast_signed_vote(itil, k.id, subject="chef", role="owner", decision="rejected")
    return itil, k, old


def _assert_rejected_everywhere(itil, k, tmp_path):
    assert _fold(itil, k.id).status.value == "rejected"
    assert cab_approval.human_approval_times(itil, _fold(itil, k.id)) == []
    assert cab_approval.approval_basis(itil, _fold(itil, k.id)) is None
    assert _gate(tmp_path, k.id) is not None
    assert trustee_actuation.change_is_approved(k.id, shared_root=tmp_path) is False


def test_replayed_old_approval_is_refused(rejected_after_approval, tmp_path):
    itil, k, old = rejected_after_approval
    with pytest.raises(ValueError, match="refusing to replace"):
        cab_approval.record_signed_vote(itil, old)
    _assert_rejected_everywhere(itil, k, tmp_path)


@pytest.mark.parametrize("slot", ["Chef", "chef "])
def test_old_approval_written_back_into_another_slot_does_not_win(
    rejected_after_approval, tmp_path, slot
):
    itil, k, old = rejected_after_approval
    vote = json.loads((itil.cab_dir / f"{k.id}-chef.json").read_text())
    vote.update(
        agent=slot,
        decision="approved",
        authorization_id=old["authorization_id"],
        authorization=old,
    )
    (itil.cab_dir / f"{k.id}-{slot}.json").write_text(json.dumps(vote))
    _assert_rejected_everywhere(itil, k, tmp_path)


def test_a_later_approval_replaces_the_rejection(rejected_after_approval, tmp_path, monkeypatch):
    itil, k, _old = rejected_after_approval
    _at(monkeypatch, 0)
    cast_signed_vote(itil, k.id, subject="chef", role="owner")
    assert cab_approval.approval_basis(itil, _fold(itil, k.id)) == "human"


def test_a_grant_issued_at_the_same_time_does_not_replace(rejected_after_approval):
    itil, k, _old = rejected_after_approval
    same = envelope(k.id, issuer="chef", role="owner")  # ISSUED_AT still an hour ago
    with pytest.raises(ValueError, match="refusing to replace"):
        cab_approval.record_signed_vote(itil, same)


def test_identity_key_strips_and_case_folds():
    key = cab_approval.identity_key
    assert key(" Chef ") == key("chef") == key("CHEF") == key("capauth:Chef @skworld.io")
    assert key("STRASSE") == key("strasse")


def test_unsigned_vote_under_another_spelling_cannot_take_the_slot(rejected_after_approval):
    itil, k, _old = rejected_after_approval
    for spelling in ("Chef", "chef "):
        with pytest.raises(ValueError, match="refusing to replace"):
            cab_approval.refuse_unsigned_overwrite(itil, k.id, spelling)


def test_the_latest_signed_vote_wins_whichever_slot_it_is_in(
    rejected_after_approval, tmp_path, monkeypatch
):
    """Per human, the latest signed issued_at counts: a newer approval in a second
    slot (``Chef``) beats the older rejection in ``chef``, as one slot would."""
    itil, k, _old = rejected_after_approval
    _at(monkeypatch, 0)
    new = envelope(k.id, issuer="chef", role="owner")
    vote = json.loads((itil.cab_dir / f"{k.id}-chef.json").read_text())
    vote.update(
        agent="Chef",
        decision="approved",
        authorization_id=new["authorization_id"],
        authorization=new,
    )
    (itil.cab_dir / f"{k.id}-Chef.json").write_text(json.dumps(vote))
    # The gates' own reading (an older skcoord fold still reads the change rejected,
    # so approval_basis, which also needs the fold's status, is not asserted here).
    assert cab_approval.human_approval_times(itil, _fold(itil, k.id)) == [new["issued_at"]]
    assert [v.agent for v in cab_approval.current_votes(itil, k.id)] == ["Chef"]
