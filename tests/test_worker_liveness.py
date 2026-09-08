from datetime import datetime, timedelta, timezone

from skcapstone.fleet.worker_liveness import (
    LivenessObservation,
    WorkerProjection,
    classify,
    metrics,
    reconcile,
)

NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)


def obs(**changes: object) -> LivenessObservation:
    values: dict[str, object] = {
        "owner": "agent",
        "card_id": "card",
        "claim_generation": "gen",
        "process_identity": "pid",
        "session_id": "session",
        "managed_session": True,
        "process_alive": True,
        "session_alive": True,
        "live_children": 0,
        "child_activity_at": None,
        "heartbeat_at": NOW - timedelta(hours=2),
        "terminal_marker": None,
        "terminal_at": None,
        "skmail_response_at": None,
        "assistance_requested_at": None,
        "workspace_recoverable": True,
        "workspace_custody": "workspace",
    }
    values.update(changes)
    return LivenessObservation(**values)  # type: ignore[arg-type]


def test_quiet_tool_wait_is_waiting_not_dead_or_assistance_due() -> None:
    decision = classify(obs(quiet_tool_wait=True), now=NOW)
    assert decision.state == "waiting"
    assert decision.assistance_request is None
    assert not decision.retire and not decision.quarantine


def test_structured_assistance_has_exact_fence_and_checkpoint_deadline() -> None:
    decision = classify(obs(), now=NOW)
    request = decision.assistance_request
    assert decision.state == "assistance-due"
    assert request is not None
    assert (request.owner, request.card_id, request.claim_generation, request.session_id) == (
        "agent",
        "card",
        "gen",
        "session",
    )
    assert request.requested_at == NOW
    assert request.checkpoint_due_at == NOW + timedelta(minutes=15)


def test_recent_child_activity_prevents_false_assistance() -> None:
    decision = classify(obs(child_activity_at=NOW - timedelta(minutes=5)), now=NOW)
    assert decision.state == "active-compute"
    assert decision.assistance_request is None


def test_nonresponsive_worker_is_quarantined_after_checkpoint() -> None:
    requested = NOW - timedelta(minutes=16)
    decision = classify(obs(assistance_requested_at=requested), now=NOW)
    assert decision.state == "checkpoint-missed"
    assert decision.quarantine and not decision.retire
    assert decision.assistance_request is not None
    assert decision.assistance_request.checkpoint_due_at == requested + timedelta(minutes=15)


def test_retirement_requires_terminal_children_custody_and_preserved_commits() -> None:
    safe = classify(obs(terminal_marker="PASS", terminal_at=NOW), now=NOW)
    assert safe.state == "retirement-ready" and safe.retire and not safe.quarantine
    for unsafe in (
        obs(terminal_marker="PASS", live_children=1),
        obs(terminal_marker="PASS", workspace_recoverable=False),
        obs(terminal_marker="PASS", workspace_custody=None),
        obs(terminal_marker="PASS", unpushed_commits=("abc123",)),
    ):
        decision = classify(unsafe, now=NOW)
        assert decision.state == "terminal-orphan"
        assert decision.quarantine and decision.preserve_workspace and not decision.retire
    preserved = classify(
        obs(
            terminal_marker="PASS_FOR_REVIEW",
            unpushed_commits=("abc123",),
            unpushed_commits_preserved=True,
        ),
        now=NOW,
    )
    assert preserved.retire and preserved.preserve_workspace


def test_dead_pane_stale_claim_high_cpu_without_identity_and_interruption_fail_closed() -> None:
    cases = (
        (obs(session_alive=False), "dead-pane"),
        (obs(process_alive=False), "dead-process"),
        (obs(process_alive=None), "ambiguous"),
        (obs(claim_active=False), "stale-claim"),
        (obs(process_identity=None, cpu_percent=99.0), "ambiguous"),
        (obs(interrupted=True), "interrupted"),
    )
    for observation, state in cases:
        decision = classify(observation, now=NOW)
        assert decision.state == state
        assert decision.quarantine and decision.preserve_workspace and not decision.retire


def test_partial_cleanup_failure_never_retires() -> None:
    decision = classify(obs(terminal_marker="PASS", cleanup_failed=True), now=NOW)
    assert decision.state == "cleanup-failed"
    assert decision.quarantine and not decision.retire


def test_terminal_projection_reconciliation_is_exact_and_idempotent() -> None:
    projections = (
        WorkerProjection("agent", "card", "gen", "active"),
        WorkerProjection("agent", "other", "other-gen", "ready"),
    )
    decision = classify(obs(terminal_marker="BLOCKED"), now=NOW)
    decisions = {("agent", "card", "gen"): decision}
    once = reconcile(projections, decisions)
    twice = reconcile(once, decisions)
    assert once == twice
    assert once[0].state == "terminal"
    assert once[1].state == "ready"


def test_metrics_are_deterministic() -> None:
    rows = (
        classify(obs(), now=NOW),
        classify(obs(quiet_tool_wait=True), now=NOW),
        classify(obs(terminal_marker="PASS", live_children=2), now=NOW),
        classify(obs(child_activity_at=NOW), now=NOW),
    )
    result = metrics(
        rows,
        assistance_latencies=(2, 4),
        retirement_latencies=(3,),
        preserved_work=(True, False),
    )
    assert result == {
        "long_runner_count": 4,
        "active_compute_count": 1,
        "waiting_count": 1,
        "terminal_orphan_count": 1,
        "assistance_latency": 3,
        "retirement_latency": 3,
        "preserved_work_outcomes": 1,
    }
