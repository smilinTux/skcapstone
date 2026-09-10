"""Deterministic worker lease, timeout, and attribution tests."""

from datetime import datetime, timedelta, timezone

import pytest

import skcapstone.fleet.worker_watchdog as worker_watchdog
from skcapstone.fleet.worker_watchdog import (
    DEFAULT_HEARTBEAT_TIMEOUT_S,
    DEFAULT_TRANSPORT_TIMEOUT_S,
    HOST_LOCAL_BEAT_NOTICE_S,
    MEASURED_CROSS_HOST_P95_S,
    GatewayRequest,
    StartupObservation,
    WorkerClassification,
    WorkerGeneration,
    WorkerObservation,
    classify_startup,
    classify_worker,
    correlate_request,
    duplicate_worker_owners,
    startup_actuation_fenced,
    summarize_workers,
)

NOW = datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc)


def _startup(**changes: object) -> StartupObservation:
    values: dict[str, object] = {
        "owner": "pi-codex-chiap08-abcd1234",
        "card_id": "card-1",
        "session_id": "session-1",
        "claim_revision": "revision-1",
        "expected_claim_revision": "revision-1",
        "heartbeat_seen": True,
        "executable_evidence_seen": True,
        "heartbeat_at": "2026-09-04T13:59:30Z",
        "executable_evidence": {
            "kind": "executable-work",
            "owner": "pi-codex-chiap08-abcd1234",
            "card_id": "card-1",
            "session_id": "session-1",
            "claim_revision": "revision-1",
        },
    }
    values.update(changes)
    return StartupObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "startup-ready"),
        ({"heartbeat_seen": False}, "startup-heartbeat-missing"),
        ({"executable_evidence_seen": False}, "startup-evidence-missing"),
        ({"claim_revision": "other"}, "startup-claim-mismatch"),
        ({"session_id": ""}, "startup-invalid-identity"),
        ({"heartbeat_at": "not-a-time"}, "startup-heartbeat-malformed"),
        ({"executable_evidence": None}, "startup-evidence-malformed"),
        (
            {"heartbeat_at": "2026-09-04T13:00:00Z"},
            "startup-heartbeat-stale",
        ),
    ],
)
def test_startup_requires_heartbeat_and_executable_evidence(
    changes: dict[str, object], expected: str
) -> None:
    assert classify_startup(_startup(**changes), now=NOW) == expected


def test_startup_release_requires_exact_fence_and_absent_process() -> None:
    observation = _startup(process_alive=False, session_alive=False)
    assert (
        startup_actuation_fenced(
            observation,
            owner=observation.owner,
            claim_revision=observation.claim_revision,
            now=NOW,
        )
        is False
    )
    stale = _startup(
        process_alive=False,
        session_alive=False,
        heartbeat_at="2026-09-04T13:00:00Z",
    )
    assert (
        startup_actuation_fenced(
            stale,
            owner=stale.owner,
            claim_revision=stale.claim_revision,
            now=NOW,
        )
        is True
    )
    assert (
        startup_actuation_fenced(stale, owner=stale.owner, claim_revision="wrong", now=NOW)
        is False
    )


INVARIANT = (
    "No lease state is derived from beat evidence alone; beats only "
    "corroborate preconditioned claim events."
)


def _worker(**changes: object) -> WorkerObservation:
    values: dict[str, object] = {
        "owner": "pi-codex-chiap08-abcd1234",
        "claim_owner": "pi-codex-chiap08-abcd1234",
        "claim_revision": "revision-1",
        "expected_claim_revision": "revision-1",
        "process_alive": True,
        "session_alive": True,
        "heartbeat_at": "2026-09-04T13:59:30Z",
        "heartbeat_source_host": "chiap08",
        "heartbeat_received_at": "2026-09-04T13:59:45Z",
        "observer_host": "chiap08",
        "unit_active": True,
        "lease_expires_at": "2026-09-04T14:05:00Z",
        "session_id": "session-1",
        "card_id": "card-1",
        "host": "chiap08",
        "lane": "codex",
        "model": "model-a",
    }
    values.update(changes)
    return WorkerObservation(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changes", "state", "reason"),
    [
        ({}, "running", "heartbeat-fresh"),
        ({"heartbeat_at": None, "unit_active": False}, "stalled", "heartbeat-absent"),
        (
            {"heartbeat_at": "2026-09-04T13:45:00Z", "heartbeat_timeout": True},
            "worker-stale",
            "worker-stale",
        ),
        ({"lease_expires_at": "2026-09-04T13:59:59Z"}, "timed-out", "lease-expired"),
        (
            {"process_alive": False, "session_alive": False},
            "exited",
            "no-process-or-session",
        ),
        ({"claim_owner": "other"}, "claim-mismatch", "owner-mismatch"),
        ({"claim_revision": "revision-2"}, "claim-mismatch", "claim-revision-mismatch"),
        (
            {"claim_revision": "", "expected_claim_revision": ""},
            "claim-mismatch",
            "claim-revision-missing",
        ),
        ({"owner": "", "claim_owner": ""}, "claim-mismatch", "owner-missing"),
    ],
)
def test_classifies_each_state_without_mutation(
    changes: dict[str, object], state: str, reason: str
) -> None:
    kwargs = dict(changes)
    use_short_timeout = kwargs.pop("heartbeat_timeout", False)
    timeout = 120 if use_short_timeout else DEFAULT_HEARTBEAT_TIMEOUT_S
    result = classify_worker(_worker(**kwargs), now=NOW, heartbeat_timeout_s=timeout)
    assert (result.state, result.reason) == (state, reason)


def test_summary_contains_all_states_and_stable_counts() -> None:
    result = summarize_workers(
        [
            _worker(),
            _worker(heartbeat_at=None, unit_active=False),
            _worker(process_alive=False, session_alive=False),
        ],
        now=NOW,
    )
    assert result["running"] == 1
    assert result["stalled"] == 1
    assert result["exited"] == 1
    assert result["telemetry-fault"] == 0
    assert result["transport-stale"] == 0
    assert result["worker-stale"] == 0


def test_classification_carries_full_worker_identity() -> None:
    result = classify_worker(_worker(), now=NOW)
    assert result.claim_revision == "revision-1"
    assert result.expected_claim_revision == "revision-1"
    assert (result.session_id, result.card_id) == ("session-1", "card-1")
    assert (result.host, result.lane, result.model) == (
        "chiap08",
        "codex",
        "model-a",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"claim_revision": None},
        {"expected_claim_revision": None},
        {"claim_revision": "", "expected_claim_revision": "revision-1"},
        {"claim_revision": "revision-1", "expected_claim_revision": ""},
    ],
)
def test_missing_or_ambiguous_claim_revision_fails_closed(
    changes: dict[str, object],
) -> None:
    result = classify_worker(_worker(**changes), now=NOW)
    assert (result.state, result.reason) == (
        "claim-mismatch",
        "claim-revision-missing",
    )


def test_transport_stale_distinct_from_worker_stale() -> None:
    transport = classify_worker(
        _worker(
            heartbeat_at=(NOW - timedelta(seconds=292)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            heartbeat_source_host="chiap04",
            observer_host="chiap01",
            host="chiap04",
        ),
        now=NOW,
    )
    worker = classify_worker(
        _worker(
            heartbeat_at=(NOW - timedelta(seconds=601)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            heartbeat_source_host="chiap08",
            observer_host="chiap08",
        ),
        now=NOW,
    )
    assert transport.state == "transport-stale"
    assert worker.state == "worker-stale"
    assert transport.state != worker.state


def test_defaults_exceed_measured_cross_host_p95() -> None:
    assert DEFAULT_HEARTBEAT_TIMEOUT_S > MEASURED_CROSS_HOST_P95_S
    assert DEFAULT_TRANSPORT_TIMEOUT_S > MEASURED_CROSS_HOST_P95_S
    assert HOST_LOCAL_BEAT_NOTICE_S < MEASURED_CROSS_HOST_P95_S


def test_remote_beat_292s_is_transport_stale_never_dead() -> None:
    """AC: beat 292s old from a remote host => transport-stale, never dead."""
    beat_at = (NOW - timedelta(seconds=292)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result = classify_worker(
        _worker(
            heartbeat_at=beat_at,
            heartbeat_source_host="chiap04",
            observer_host="chiap01",
            host="chiap04",
        ),
        now=NOW,
    )
    assert result.state == "transport-stale"
    assert result.reason == "transport-stale"
    assert result.state not in {"timed-out", "exited", "worker-stale"}
    assert result.releasable is False


def test_absent_beat_with_active_unit_is_telemetry_fault_never_releasable() -> None:
    """AC: beat absent + host-local unit active => telemetry fault, never releasable."""
    result = classify_worker(
        _worker(heartbeat_at=None, unit_active=True),
        now=NOW,
    )
    assert result.state == "telemetry-fault"
    assert result.reason == "beat-absent-unit-active"
    assert result.releasable is False


def test_module_docstring_carries_verbatim_invariant() -> None:
    assert worker_watchdog.__doc__ is not None
    assert INVARIANT in " ".join(worker_watchdog.__doc__.split())


def test_no_actuation_symbols_in_module() -> None:
    names = {name.lower() for name in dir(worker_watchdog)}
    for forbidden in ("release", "reap", "terminate", "kill"):
        assert not any(forbidden in name for name in names)


def test_request_join_is_explicit_and_value_free() -> None:
    worker = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "revision-1",
        "revision-1",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    request = GatewayRequest(
        "request-1", "agent-a", "session-1", "card-1", "revision-1", "chiap08", "codex", "model-a"
    )
    result = correlate_request(request, {"agent-a": worker})
    assert result.state == "running"
    assert result.worker_owner == "agent-a"
    assert result.missing == ()
    incomplete = correlate_request(
        GatewayRequest("request-2", None, None, None, None, "chiap08", "codex", "model-a"),
        {"agent-a": worker},
    )
    assert incomplete.state == "unmatched"
    assert "agent_id" in incomplete.missing


def test_request_stale_revision_is_claim_mismatch() -> None:
    worker = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "current",
        "current",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    result = correlate_request(
        GatewayRequest(
            "request-1",
            "agent-a",
            "session-1",
            "card-1",
            "stale",
            "chiap08",
            "codex",
            "model-a",
        ),
        {"agent-a": worker},
    )
    assert result.state == "claim-mismatch"
    assert result.reason == "claim-revision-mismatch"


def test_request_partial_claim_identity_is_incomplete() -> None:
    worker = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "current",
        "current",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    result = correlate_request(
        GatewayRequest(
            "request-1",
            "agent-a",
            "session-1",
            "card-1",
            None,
            "chiap08",
            "codex",
            "model-a",
        ),
        {"agent-a": worker},
    )
    assert result.state == "incomplete"
    assert result.missing == ("claim_revision",)


def test_duplicate_generation_is_fenced_by_full_identity() -> None:
    assert duplicate_worker_owners([_worker(), _worker(claim_revision="revision-2")]) == (
        WorkerGeneration(
            "pi-codex-chiap08-abcd1234",
            "chiap08",
            "card-1",
            ("revision-1", "revision-2"),
            2,
        ),
    )
    assert duplicate_worker_owners([_worker(), _worker(host="chiap03")]) == ()


def test_ambiguous_generation_is_not_reported_as_duplicate() -> None:
    assert duplicate_worker_owners([_worker(card_id=None), _worker()]) == ()


def test_reassignment_revision_fences_old_request() -> None:
    old = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "old",
        "old",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    reassigned = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "new",
        "new",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    request = GatewayRequest(
        "request-1",
        "agent-a",
        "session-1",
        "card-1",
        "old",
        "chiap08",
        "codex",
        "model-a",
    )
    assert correlate_request(request, {"agent-a": reassigned}).state == "claim-mismatch"
    assert correlate_request(request, {"agent-a": old}).state == "running"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_id", "other-session"),
        ("card_id", "other-card"),
        ("host", "chiap03"),
        ("lane", "qwen"),
        ("model", "other-model"),
    ],
)
def test_request_identity_fields_must_match_worker(field: str, value: str) -> None:
    worker = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "rev",
        "rev",
        "session-1",
        "card-1",
        "chiap08",
        "codex",
        "model-a",
    )
    request_values = {
        "request_id": "request-1",
        "agent_id": "agent-a",
        "session_id": "session-1",
        "card_id": "card-1",
        "claim_revision": "rev",
        "host": "chiap08",
        "lane": "codex",
        "model": "model-a",
    }
    request_values[field] = value
    result = correlate_request(GatewayRequest(**request_values), {"agent-a": worker})
    assert result.state == "unmatched"
    assert result.reason == "identity-mismatch:%s" % field


def test_incomplete_worker_identity_fails_closed() -> None:
    worker = WorkerClassification(
        "agent-a",
        "running",
        "heartbeat-fresh",
        "rev",
        "rev",
        "session-1",
        None,
        "chiap08",
        "codex",
        "model-a",
    )
    request = GatewayRequest(
        "request-1",
        "agent-a",
        "session-1",
        "card-1",
        "rev",
        "chiap08",
        "codex",
        "model-a",
    )
    result = correlate_request(request, {"agent-a": worker})
    assert result.state == "incomplete"
    assert result.missing == ("card_id",)
