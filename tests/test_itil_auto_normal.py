"""Operator Seat auto-normal tier: the operator may auto-approve a NORMAL change
only under the full predicate; the human veto and the major-needs-human rule hold."""

from __future__ import annotations

from pathlib import Path

from skcapstone.itil import ITILManager


def _op_change(mgr, **kw):
    base = dict(
        title="auto fix",
        change_type="normal",
        risk="low",
        rollback_plan="revert placement",
        created_by="operator",
        tags=["auto-normal"],
    )
    base.update(kw)
    return mgr.propose_change(**base)


def test_auto_normal_operator_change_auto_approves(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr)
    assert chg.status.value == "approved"


def test_auto_normal_high_risk_not_auto_approved(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr, risk="high")
    assert chg.status.value == "proposed"


def test_auto_normal_no_rollback_not_auto_approved(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr, rollback_plan="")
    assert chg.status.value == "proposed"


def test_auto_normal_not_operator_authored_not_auto_approved(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr, created_by="lumina")
    assert chg.status.value == "proposed"


def test_auto_normal_missing_tag_needs_human(tmp_path: Path):
    # No auto-normal tag == an operator MAJOR action: it must NOT self-approve.
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr, tags=[])
    assert chg.status.value == "proposed"


def test_auto_normal_rejection_still_blocks(tmp_path: Path):
    mgr = ITILManager(tmp_path)
    chg = _op_change(mgr)
    # Even an already-auto-approved eligible change: a human rejection vote blocks.
    mgr.submit_cab_vote(chg.id, agent="human", decision="rejected")
    assert mgr.list_changes()[0].status.value == "rejected"
