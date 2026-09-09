import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from skcapstone.fleet.worker_liveness import (
    LivenessObservation,
    RuntimeActions,
    SQLiteReceiptJournal,
    WorkerProjection,
    classify,
    metrics,
    reconcile,
    run_cycle,
)

NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)


def obs(**changes: object) -> LivenessObservation:
    values: dict[str, object] = {
        "host": "chiap08",
        "observer_host": "chiap08",
        "observed_at": NOW,
        "owner": "agent",
        "card_id": "card",
        "claim_generation": "gen",
        "process_identity": "pid:123",
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
        "current_claim_generation": "gen",
        "cgroup_processes": 0,
        "unit": "skfleet-worker-codex-card.service",
        "pid": 123,
        "process_tree": (123,),
        "cgroup": "/user.slice/skfleet-worker-codex-card.service",
        "process_observed_at": NOW,
        "cgroup_observed_at": NOW,
        "beat_id": "beat-1",
        "workspace_path": "/work/card",
        "workspace_repository": "smilinTux/skcapstone",
        "workspace_head": "a" * 40,
        "workspace_custody_at": NOW,
        "workspace_custody_sha256": "b" * 64,
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
    safe = classify(
        obs(terminal_marker="PASS", terminal_at=NOW, heartbeat_at=NOW),
        now=NOW,
        authorize_retirement=lambda _: True,
    )
    assert safe.state == "retirement-ready" and safe.retire and not safe.quarantine
    for unsafe in (
        obs(terminal_marker="PASS", terminal_at=NOW, live_children=1),
        obs(terminal_marker="PASS", terminal_at=NOW, cgroup_processes=1),
        obs(terminal_marker="PASS", terminal_at=NOW, workspace_recoverable=False),
        obs(terminal_marker="PASS", terminal_at=NOW, workspace_custody=None),
        obs(terminal_marker="PASS", terminal_at=NOW, unpushed_commits=("abc123",)),
    ):
        decision = classify(unsafe, now=NOW, authorize_retirement=lambda _: True)
        assert decision.state == "terminal-orphan"
        assert decision.quarantine and decision.preserve_workspace and not decision.retire
    preserved = classify(
        obs(
            terminal_marker="PASS_FOR_REVIEW",
            terminal_at=NOW,
            heartbeat_at=NOW,
            unpushed_commits=("abc123",),
            unpushed_commits_preserved=True,
        ),
        now=NOW,
        authorize_retirement=lambda _: True,
    )
    assert preserved.retire and preserved.preserve_workspace


def test_retirement_is_fenced_to_current_claim_generation_and_cgroup_truth() -> None:
    missing_generation = classify(obs(current_claim_generation=None), now=NOW)
    changed_generation = classify(obs(current_claim_generation="new-gen"), now=NOW)
    missing_cgroup = classify(obs(cgroup_processes=None), now=NOW)
    missing_terminal_time = classify(obs(terminal_marker="PASS"), now=NOW)

    assert missing_generation.state == "stale-claim"
    assert changed_generation.state == "stale-claim"
    assert missing_cgroup.state == "ambiguous"
    assert missing_terminal_time.state == "terminal-orphan"
    for decision in (
        missing_generation,
        changed_generation,
        missing_cgroup,
        missing_terminal_time,
    ):
        assert decision.quarantine and decision.preserve_workspace
        assert not decision.retire


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
    decision = classify(obs(terminal_marker="PASS", terminal_at=NOW, cleanup_failed=True), now=NOW)
    assert decision.state == "cleanup-failed"
    assert decision.quarantine and not decision.retire


def test_terminal_projection_reconciliation_is_exact_and_idempotent() -> None:
    projections = (
        WorkerProjection("agent", "card", "gen", "active"),
        WorkerProjection("agent", "other", "other-gen", "ready"),
    )
    decision = classify(
        obs(terminal_marker="BLOCKED", terminal_at=NOW, heartbeat_at=NOW),
        now=NOW,
        authorize_retirement=lambda _: True,
    )
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
        classify(obs(terminal_marker="PASS", terminal_at=NOW, live_children=2), now=NOW),
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


def test_runtime_connects_assistance_reconciliation_metrics_and_retirement() -> None:
    calls: dict[str, list[object]] = {
        "assistance": [],
        "reconciliation": [],
        "metrics": [],
        "retirement": [],
    }
    actions = RuntimeActions(
        calls["assistance"].append,
        calls["reconciliation"].append,
        calls["metrics"].append,
        calls["retirement"].append,
        lambda _: True,
    )
    terminal = obs(terminal_marker="PASS", terminal_at=NOW, heartbeat_at=NOW)
    waiting = obs(
        card_id="waiting", claim_generation="wait-gen", current_claim_generation="wait-gen"
    )
    result = run_cycle(
        (terminal, waiting),
        (
            WorkerProjection("agent", "card", "gen", "active"),
            WorkerProjection("agent", "waiting", "wait-gen", "active"),
        ),
        now=NOW,
        actions=actions,
    )
    assert len(calls["assistance"]) == 1
    assert [row.state for row in calls["reconciliation"]] == ["terminal", "active"]
    assert calls["metrics"] == [result.metric_values]
    assert calls["retirement"] == list(result.receipts)
    receipt = result.receipts[0]
    assert (
        receipt.host,
        receipt.observer_host,
        receipt.unit,
        receipt.pid,
        receipt.process_tree,
        receipt.cgroup,
        receipt.claim_generation,
        receipt.beat_id,
        receipt.workspace_custody,
    ) == (
        "chiap08",
        "chiap08",
        "skfleet-worker-codex-card.service",
        123,
        (123,),
        "/user.slice/skfleet-worker-codex-card.service",
        "gen",
        "beat-1",
        "workspace",
    )


def test_incomplete_receipt_quarantines_and_never_calls_retirement() -> None:
    retired: list[object] = []
    actions = RuntimeActions(
        lambda _: None, lambda _: None, lambda _: None, retired.append, lambda _: True
    )
    for changes in (
        {"host": ""},
        {"observer_host": "chiap01"},
        {"unit": None},
        {"pid": None},
        {"process_tree": ()},
        {"process_tree": (999,)},
        {"cgroup": None},
        {"process_observed_at": None},
        {"cgroup_observed_at": None},
        {"beat_id": None},
        {"heartbeat_at": None},
        {"workspace_custody": None},
        {"workspace_path": None},
        {"workspace_repository": None},
        {"workspace_head": None},
        {"workspace_custody_at": None},
        {"workspace_custody_sha256": None},
    ):
        result = run_cycle(
            (obs(terminal_marker="PASS", terminal_at=NOW, **changes),),
            (),
            now=NOW,
            actions=actions,
        )
        assert result.decisions[0].quarantine
        assert not result.decisions[0].retire
    assert retired == []


def test_healthy_worker_is_never_retired_by_runtime() -> None:
    retired: list[object] = []
    actions = RuntimeActions(
        lambda _: None, lambda _: None, lambda _: None, retired.append, lambda _: True
    )
    result = run_cycle((obs(child_activity_at=NOW),), (), now=NOW, actions=actions)
    assert result.decisions[0].state == "active-compute"
    assert retired == []


def test_sqlite_receipt_journal_tolerates_concurrent_writers(tmp_path) -> None:
    journal = SQLiteReceiptJournal(tmp_path / "runtime" / "retirement.sqlite3")
    receipt = run_cycle(
        (obs(terminal_marker="PASS", terminal_at=NOW, heartbeat_at=NOW),),
        (),
        now=NOW,
        actions=RuntimeActions(
            lambda _: None, lambda _: None, lambda _: None, lambda _: None, lambda _: True
        ),
    ).receipts[0]
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda _: journal.append(receipt), range(100)))
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM retirement_receipts").fetchone() == (1,)
