from datetime import datetime, timedelta, timezone

from skcapstone.fleet.worker_liveness import LivenessObservation, classify, metrics

NOW = datetime(2026, 9, 8, tzinfo=timezone.utc)


def obs(**kw):
    base = dict(
        owner="agent",
        card_id="card",
        claim_generation="gen",
        process_identity="pid",
        managed_session=True,
        live_children=0,
        heartbeat_at=NOW - timedelta(hours=2),
        terminal_marker=None,
        skmail_response_at=None,
        workspace_recoverable=True,
        workspace_custody="workspace",
    )
    base.update(kw)
    return LivenessObservation(**base)


def test_quiet_tool_wait_is_not_death():
    d = classify(obs(quiet_tool_wait=True), now=NOW)
    assert d.state == "waiting" and not d.escalate and not d.retire


def test_assistance_requires_attribution_and_hour():
    d = classify(obs(), now=NOW)
    assert d.state == "assistance-due" and d.escalate
    assert classify(obs(process_identity=None), now=NOW).quarantine


def test_terminal_retirement_requires_all_proofs():
    d = classify(obs(terminal_marker="PASS"), now=NOW)
    assert d.retire and not d.quarantine
    assert classify(obs(terminal_marker="PASS", live_children=1), now=NOW).quarantine
    assert classify(obs(terminal_marker="PASS", workspace_recoverable=False), now=NOW).quarantine


def test_interruption_is_quarantined():
    assert classify(obs(interrupted=True), now=NOW).quarantine


def test_metrics_are_deterministic():
    rows = [
        classify(obs(), now=NOW),
        classify(obs(quiet_tool_wait=True), now=NOW),
        classify(obs(terminal_marker="PASS", live_children=2), now=NOW),
    ]
    m = metrics(
        rows, assistance_latencies=[2, 4], retirement_latencies=[3], preserved_work=[True, False]
    )
    assert m["long_runner_count"] == 3
    assert m["active_compute_count"] == 0
    assert m["waiting_count"] == 1
    assert m["terminal_orphan_count"] == 1
    assert m["assistance_latency"] == 3
    assert m["preserved_work_outcomes"] == 1
