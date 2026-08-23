"""Tests for the CM P3.1 change-deploy runner (skcapstone.change_deploy).

Phase 3a of the change-management design
(docs/specs/2026-08-13-change-management-cab-ai-arch.md sections 5.2/5.3):
a scheduled-job scanner in PLAN-ONLY CANARY. It must never deploy anything -
these tests prove the seam stays unwired by default, a would-deploy plan is
recorded instead of a dispatch, missed windows fold back to approved, and a
per-change lease prevents a double-fire.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone import change_deploy as cd
from skcapstone.itil import ITILManager


def _approve_via_human_vote(mgr: ITILManager, change_id: str) -> None:
    mgr.submit_cab_vote(change_id, agent="human", decision="approved")


def _scheduled_change(mgr: ITILManager, window_start: str, window_end: str, **kw):
    base = dict(title="cutover the thing", change_type="normal", managed_by="lumina")
    base.update(kw)
    chg = mgr.propose_change(**base)
    _approve_via_human_vote(mgr, chg.id)
    mgr._append_event(
        mgr.changes_dir,
        chg.id,
        "operator",
        "schedule",
        window_start=window_start,
        window_end=window_end,
        asap=False,
        deploy_mode="confirm",
    )
    return mgr.list_changes()[0]


@pytest.fixture(autouse=True)
def _no_deploy_dispatcher():
    """Every test starts (and ends) with the seam unwired, regardless of test
    order - mirrors the discipline test_agent_run.py needs for the R1 seam."""
    cd.set_deploy_dispatcher(None)
    yield
    cd.set_deploy_dispatcher(None)


@pytest.fixture
def bridge_absent(monkeypatch):
    """Simulate the skharness deploy bridge not being importable.

    These tests assert the fail-closed path taken when the bridge is missing.
    They originally relied on it genuinely not existing yet, which stopped
    being true once skharness shipped change_deploy_bridge (P3.2): CI installs
    siblings from git main, so the ambient environment silently flipped the
    premise and the assertions started failing on main. Simulate absence
    explicitly so the fail-closed path is tested either way.
    """
    monkeypatch.setitem(sys.modules, "skharness.autocode.change_deploy_bridge", None)


def test_seam_defaults_unwired():
    assert cd.deploy_dispatch_available() is False


def test_build_deploy_dispatcher_returns_none_without_bridge(bridge_absent):
    # With the bridge not importable, build_deploy_dispatcher must fail-close
    # to None, a first-class outcome, not raise. This passed incidentally once
    # skharness shipped the bridge (it refuses to build without the flag), so
    # it now states its premise explicitly.
    assert cd.build_deploy_dispatcher() is None


def test_maybe_wire_deploy_bridge_noop_without_flag(monkeypatch):
    monkeypatch.delenv("SKAI_DEPLOY_BRIDGE", raising=False)
    cd._maybe_wire_deploy_bridge()
    assert cd.deploy_dispatch_available() is False


def test_maybe_wire_deploy_bridge_noop_with_flag_but_no_bridge_installed(
    monkeypatch, bridge_absent
):
    # Even with the flag set, wiring must fail closed when the bridge is
    # not importable (see the bridge_absent fixture).
    monkeypatch.setenv("SKAI_DEPLOY_BRIDGE", "1")
    cd._maybe_wire_deploy_bridge()
    assert cd.deploy_dispatch_available() is False


def test_dispatch_deploy_refuses_when_unwired():
    result = cd._dispatch_deploy({"change_id": "chg-doesnotmatter"})
    assert result["refused"] is True


def test_list_due_finds_in_window_change(tmp_path):
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    due = cd.list_due(tmp_path, now=now)
    assert [d["change"].id for d in due] == [chg.id]


def test_list_due_excludes_future_window(tmp_path):
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now + timedelta(hours=1)).isoformat()
    end = (now + timedelta(hours=2)).isoformat()
    _scheduled_change(mgr, start, end)

    assert cd.list_due(tmp_path, now=now) == []


def test_list_missed_finds_closed_window(tmp_path):
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).isoformat()
    end = (now - timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    missed = cd.list_missed(tmp_path, now=now)
    assert [m["change"].id for m in missed] == [chg.id]
    # not also due
    assert cd.list_due(tmp_path, now=now) == []


def test_canary_records_would_deploy_plan_and_calls_no_dispatcher(tmp_path):
    """The core canary behavior: seam unwired -> a would-deploy plan lands on
    the change record's timeline, the status stays 'scheduled', and no
    dispatcher is ever invoked."""
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    # Prove the seam is unwired before the tick runs at all.
    assert cd.deploy_dispatch_available() is False

    results = cd.run_change_deploy_tick(tmp_path, now=now)
    assert results == [{"change_id": chg.id, "action": "would-deploy", "dispatched": False}]
    assert cd.deploy_dispatch_available() is False  # still unwired: nothing was ever dispatched

    folded = mgr.list_changes(status="scheduled")[0]
    assert folded.id == chg.id
    assert folded.status.value == "scheduled"  # unchanged: plan-only, no transition
    plan_rows = [r for r in folded.timeline if "would-deploy plan" in (r.get("note") or "")]
    assert len(plan_rows) == 1
    assert "Nothing executed" in plan_rows[0]["note"]


def test_window_missed_folds_back_to_approved(tmp_path):
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).isoformat()
    end = (now - timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    results = cd.run_change_deploy_tick(tmp_path, now=now)
    assert results == [{"change_id": chg.id, "action": "window_missed"}]

    folded = mgr.list_changes()[0]
    assert folded.id == chg.id
    assert folded.status.value == "approved"  # fold sends it back, never a late fire
    assert folded.scheduled_window is None

    # a subsequent tick finds nothing due or missed (no longer 'scheduled')
    again = cd.run_change_deploy_tick(tmp_path, now=now)
    assert again == []


def test_lease_prevents_double_fire(tmp_path):
    """Two back-to-back attempts to process the same due change (simulating
    an overlapping tick / a raced second runner) must not double-record a
    would-deploy plan or double-dispatch: the second is a clean skip."""
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    first = cd.claim_deploy_lease(tmp_path, chg.id, worker="tick-a")
    second = cd.claim_deploy_lease(tmp_path, chg.id, worker="tick-b")
    assert first is True
    assert second is False

    # Clear that probe lease so the process_due_change check below starts
    # clean: it must honor the lease itself, calling it twice for the same
    # window only records one would-deploy plan.
    rid = mgr._resolve_id(mgr.changes_dir, chg.id)
    (mgr.changes_dir / rid / "deploy-lease.json").unlink()
    window = mgr.list_changes()[0].scheduled_window
    out1 = cd.process_due_change(tmp_path, mgr.list_changes()[0], window, worker="runner-x")
    out2 = cd.process_due_change(tmp_path, mgr.list_changes()[0], window, worker="runner-x")
    assert out1["action"] == "would-deploy"
    assert out2 == {"change_id": chg.id, "action": "skipped", "reason": "active lease"}

    folded = mgr.list_changes()[0]
    plan_rows = [r for r in folded.timeline if "would-deploy plan" in (r.get("note") or "")]
    # exactly one plan: out1 claimed the lease and recorded it; out2 found an
    # active lease and was refused before it ever reached _dispatch_deploy.
    assert len(plan_rows) == 1


def test_lease_expires_and_allows_reclaim(tmp_path):
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    assert cd.claim_deploy_lease(tmp_path, chg.id, worker="a", lease_seconds=1) is True
    assert cd.claim_deploy_lease(tmp_path, chg.id, worker="b", lease_seconds=1) is False

    # Write an already-expired lease directly and confirm it is reclaimable.
    import json as _json

    rid = mgr._resolve_id(mgr.changes_dir, chg.id)
    lease_path = mgr.changes_dir / rid / "deploy-lease.json"
    expired = {
        "worker": "a@host",
        "change_id": chg.id,
        "claimed_at": (now - timedelta(hours=1)).isoformat(),
        "expires": (now - timedelta(minutes=1)).isoformat(),
    }
    lease_path.write_text(_json.dumps(expired), encoding="utf-8")
    assert cd.claim_deploy_lease(tmp_path, chg.id, worker="c") is True


def test_in_window_change_executes_nothing_with_seam_unwired(tmp_path, monkeypatch, bridge_absent):
    """Even with SKAI_DEPLOY_BRIDGE=1 set (but no bridge installed), an
    in-window scheduled change must still execute nothing: build returns
    None, the seam stays unwired, and the tick only records a plan."""
    monkeypatch.setenv("SKAI_DEPLOY_BRIDGE", "1")
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    chg = _scheduled_change(mgr, start, end)

    results = cd.run_change_deploy_tick(tmp_path, now=now)
    assert results == [{"change_id": chg.id, "action": "would-deploy", "dispatched": False}]
    assert cd.deploy_dispatch_available() is False

    folded = mgr.list_changes()[0]
    assert folded.status.value == "scheduled"


def test_run_change_deploy_job_smoke(tmp_path, monkeypatch):
    import skcapstone

    monkeypatch.setattr(skcapstone, "SHARED_ROOT", str(tmp_path), raising=False)
    mgr = ITILManager(tmp_path)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=5)).isoformat()
    end = (now + timedelta(hours=1)).isoformat()
    _scheduled_change(mgr, start, end)

    cd.run_change_deploy_job()  # no exception

    folded = mgr.list_changes()[0]
    assert folded.status.value == "scheduled"
    plan_rows = [r for r in folded.timeline if "would-deploy plan" in (r.get("note") or "")]
    assert len(plan_rows) == 1
