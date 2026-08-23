"""Tests for CM P2.1 (prepare carve-out in gate()) and CM P2.2 (pr_link append).

Design: docs/specs/2026-08-13-change-management-cab-ai-arch.md section 5.1.
CM P1.1/P1.2 (scheduled status, pr_link/validation/schedule events, the
change.* MCP/CLI surface) already ship on main; these tests exercise the
agent_run.py side of the wiring: gate() gaining a folded change status as an
input, and process_one() writing the pr_link event back onto the ITIL change
record when a prepare run lands a draft PR.
"""

from __future__ import annotations

import pytest

from skcapstone import agent_run as ar
from skcapstone.card_store import CardCore, CardStore
from skcapstone.itil import Change, ITILManager

_BLOCKED_REASON = (
    "change tickets require a human/CAB vote to 'approved' "
    "before implementing; the agent may draft only (no self-approval)"
)


@pytest.fixture(autouse=True)
def _reset_execute_dispatcher():
    """``_execute_dispatcher`` is module-global state; never leak it across tests."""
    yield
    ar.set_execute_dispatcher(None)


@pytest.fixture
def home(tmp_path):
    return tmp_path


# ---------------------------------------------------------------------------
# gate() decision matrix (CM P2.1)
# ---------------------------------------------------------------------------


def test_gate_allows_execute_on_proposed_or_reviewing_change():
    for status in ("proposed", "reviewing"):
        decision = ar.gate("change", "execute", change_status=status)
        assert decision["allow_execute"] is True, status
        assert "prepare" in decision["reason"].lower()


def test_gate_blocks_execute_on_every_other_change_status():
    # approved, scheduled, implementing, deployed, verified, failed, rejected,
    # closed, an unrecognized status, and an unfoldable record (None) all keep
    # today's block with today's exact reason string.
    for status in (
        None,
        "approved",
        "scheduled",
        "implementing",
        "deployed",
        "verified",
        "failed",
        "rejected",
        "closed",
        "not-a-real-status",
    ):
        decision = ar.gate("change", "execute", change_status=status)
        assert decision["allow_execute"] is False, status
        assert decision["reason"] == _BLOCKED_REASON, status


def test_gate_default_call_shape_unchanged():
    # No regression: a caller that never passes change_status (pre-P2.1 call
    # shape) still gets today's block with today's exact reason.
    assert ar.gate("change", "execute") == {
        "allow_execute": False,
        "reason": _BLOCKED_REASON,
    }


def test_gate_propose_and_dry_run_unaffected_by_change_status():
    for mode in ("propose", "dry-run"):
        decision = ar.gate("change", mode, change_status="approved")
        assert decision["allow_execute"] is True


def test_gate_gtd_origin_still_wins_over_change_status():
    # origin="gtd" is checked before the change/kind branch; a change_status
    # that would otherwise block must not override the gtd draft-only allow.
    decision = ar.gate("change", "execute", origin="gtd", change_status="approved")
    assert decision["allow_execute"] is True
    assert "gtd" in decision["reason"]


# ---------------------------------------------------------------------------
# process_one(): the gate carve-out wired to a folded ITIL change record
# ---------------------------------------------------------------------------


def test_process_one_execute_not_gated_while_proposed(home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")
    assert chg.status.value == "proposed"

    ar.request_run(home, chg.id, "prepare the cutover", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    out = ar.process_one(home, item)
    # No live dispatcher and SKAI_RUNNER_LIVE unset: falls through to the
    # "planned" path, but crucially it was never gated (allow_execute=True).
    assert out.get("gated") is not True
    assert out["state"] == "needs-review"


def test_process_one_execute_not_gated_while_reviewing(home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")
    mgr.update_change(chg.id, "lumina", new_status="reviewing")
    assert mgr._fold_record(mgr.changes_dir, chg.id, Change).status.value == "reviewing"

    ar.request_run(home, chg.id, "prepare the cutover", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    out = ar.process_one(home, item)
    assert out.get("gated") is not True
    assert out["state"] == "needs-review"


def test_process_one_execute_blocked_once_approved(home):
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")
    # Approval is reached the way a real CAB approval happens: a `human`
    # APPROVE vote through submit_cab_vote(), whose voter differs from the
    # drafter ("lumina"), so the fold's no-self-approval filter keeps it in
    # the approvals pool. A raw update_change(..., new_status="approved")
    # deliberately no longer reaches "approved" (skcoord 941570f closed that
    # self-approval bypass); see the negative control below.
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    assert mgr._fold_record(mgr.changes_dir, chg.id, Change).status.value == "approved"

    ar.request_run(home, chg.id, "implement it now", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    out = ar.process_one(home, item)
    assert out.get("gated") is True
    assert out["reason"] == _BLOCKED_REASON


def test_raw_status_event_cannot_grant_cab_approval(home):
    """Negative control for the CAB bypass guard (skcoord 941570f).

    A raw ``update_change(..., new_status="approved")`` is exactly the
    self-approval route the guard closed: the ``agent`` string is free text, so
    without the guard any caller (the MCP tool and CLI included) could approve
    its own change around ``submit_cab_vote()`` and its no-self-approval fold
    guard. It must fold as a conflict and leave the change at "proposed", even
    when the caller spells itself "human". If this test ever goes green on an
    "approved" status, the bypass is back.
    """
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")
    mgr.update_change(chg.id, "human", new_status="approved", note="CAB approved")
    assert mgr._fold_record(mgr.changes_dir, chg.id, Change).status.value == "proposed"

    # ...and because it never reached "approved", the draft-only carve-out is
    # still the live decision: no bypass leaks into the agent_run gate either.
    ar.request_run(home, chg.id, "implement it now", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    out = ar.process_one(home, {"card_id": chg.id, "kind": "change", "run": run})
    assert out.get("gated") is not True


def test_process_one_execute_blocked_when_change_record_unfoldable(home):
    # A chg- card with no ITIL record behind it (deleted, never synced, bad
    # id) must fail closed, same block as any other non-draft status.
    CardStore(home).create(
        CardCore(
            id="chg-ghost0001",
            kind="change",
            title="Ghost change (no ITIL record)",
            created_by="lumina",
        )
    )
    ar.request_run(home, "chg-ghost0001", "implement it", mode="execute")
    run = ar.current_run(home, "chg-ghost0001")
    item = {"card_id": "chg-ghost0001", "kind": "change", "run": run}

    out = ar.process_one(home, item)
    assert out.get("gated") is True
    assert out["reason"] == _BLOCKED_REASON


# ---------------------------------------------------------------------------
# pr_link append (CM P2.2)
# ---------------------------------------------------------------------------


def test_process_one_execute_appends_pr_link_to_change_record(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")

    ar.request_run(home, chg.id, "prepare the cutover", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    def dispatcher(ctx):
        return {
            "summary": "opened a draft PR",
            "activity": [{"atype": "action", "text": "ran twin-gate grader"}],
            "links": {
                "pr": "https://github.com/smilinTux/skcoord/pull/99",
                "branch": "ai/chg-cutover",
                "head_sha": "deadbeef",
            },
        }

    ar.set_execute_dispatcher(dispatcher)
    out = ar.process_one(home, item)
    assert out["state"] == "needs-review"

    folded = mgr._fold_record(mgr.changes_dir, chg.id, Change)
    assert folded.prepared_pr is not None
    assert folded.prepared_pr["url"] == "https://github.com/smilinTux/skcoord/pull/99"
    assert folded.prepared_pr["branch"] == "ai/chg-cutover"
    assert folded.prepared_pr["run_id"] == run["run_id"]
    assert folded.prepared_pr["head_sha"] == "deadbeef"
    # writer = the requesting agent of the run (the drafter), not the runner.
    assert folded.prepared_by == "lumina"

    run_after = ar.current_run(home, chg.id)
    assert run_after["links"].get("pr") == "https://github.com/smilinTux/skcoord/pull/99"


def test_process_one_pr_link_skipped_without_a_pr_link_in_result(home, monkeypatch):
    # No links.pr in the dispatcher result -> no pr_link event, no crash.
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")

    ar.request_run(home, chg.id, "prepare the cutover", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    ar.set_execute_dispatcher(lambda ctx: {"summary": "no PR yet", "activity": [], "links": {}})
    out = ar.process_one(home, item)
    assert out["state"] == "needs-review"

    folded = mgr._fold_record(mgr.changes_dir, chg.id, Change)
    assert folded.prepared_pr is None
    assert folded.prepared_by is None


def test_process_one_pr_link_append_failure_does_not_fail_run(home, monkeypatch):
    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    mgr = ITILManager(home)
    chg = mgr.propose_change(title="Cutover skgateway", created_by="lumina")

    ar.request_run(home, chg.id, "prepare the cutover", agent="lumina", mode="execute")
    run = ar.current_run(home, chg.id)
    item = {"card_id": chg.id, "kind": "change", "run": run}

    ar.set_execute_dispatcher(
        lambda ctx: {
            "summary": "opened a draft PR",
            "activity": [],
            "links": {
                "pr": "https://github.com/smilinTux/skcoord/pull/100",
                "branch": "ai/chg-x",
            },
        }
    )

    def boom(self, *args, **kwargs):
        raise OSError("simulated disk failure appending the event")

    monkeypatch.setattr(ITILManager, "_append_event", boom)

    out = ar.process_one(home, item)
    # The run itself still succeeds; only the change-linkage step failed.
    assert out["state"] == "needs-review"
    assert "error" not in out

    run_after = ar.current_run(home, chg.id)
    # links are still recorded on the run/card (the PR was really opened).
    assert run_after["links"].get("pr") == "https://github.com/smilinTux/skcoord/pull/100"
    # ...but the failed change-linkage is visible as an activity entry.
    assert any(
        a["atype"] == "error" and "change-linkage failed" in a["text"]
        for a in run_after["activity"]
    )


def test_process_one_pr_link_only_applies_to_change_kind_execute(home, monkeypatch):
    # A plain task's execute run must never try to touch the ITIL change
    # store, even if its dispatcher result happens to include links.pr.
    from skcapstone.coordination import Board, Task

    monkeypatch.setenv("SKAI_RUNNER_LIVE", "1")
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id="t1", title="Wire 2FA verify", created_by="opus"))
    from skcapstone.card_store import import_from_legacy

    import_from_legacy(home)

    ar.request_run(home, "t1", "add tests and open a PR", agent="opus", mode="execute")
    run = ar.current_run(home, "t1")
    item = {"card_id": "t1", "kind": "task", "run": run}

    ar.set_execute_dispatcher(
        lambda ctx: {
            "summary": "opened a draft PR",
            "activity": [],
            "links": {"pr": "#7", "branch": "feat/x"},
        }
    )
    out = ar.process_one(home, item)
    assert out["state"] == "needs-review"
    # no ITIL change directory was created as a side effect
    mgr = ITILManager(home)
    assert mgr.list_changes() == []
