from __future__ import annotations

from pathlib import Path

import pytest

from skdashboard.control_plane_fixture import REPORT
from skdashboard.dashboard_reports import ReportSnapshotStore
from skdashboard.report_delivery import (
    DELIVERY_CAPABILITY,
    DeliveryApproval,
    ReportDeliveryConflict,
    ReportDeliveryDenied,
    ReportDeliveryError,
    ReportDeliveryService,
    SimulationDestination,
)

NOW = "2026-08-24T12:00:00Z"
SCHEDULED = "2026-08-24T12:05:00Z"
EXPIRES = "2026-08-24T13:00:00Z"


def service(tmp_path: Path, *, allowed=True, store=None) -> ReportDeliveryService:
    snapshots = store or ReportSnapshotStore(tmp_path)
    if store is None:
        snapshots.put(REPORT)
    return ReportDeliveryService(
        tmp_path,
        policy_checker=lambda _approval: allowed,
        snapshot_store=snapshots,
    )


def draft(delivery: ReportDeliveryService, **overrides) -> dict:
    values = {
        "snapshot_id": REPORT["snapshot_id"],
        "destination_id": "sim.review-room",
        "audience": tuple(REPORT["audience"]),
        "classification": "public",
        "source_rights_ref": "rights:synthetic-approved",
        "purpose": "report.review.simulation",
        "retention_days": 30,
        "redaction_profile": "exact_snapshot",
        "scheduled_for": SCHEDULED,
        "expires_at": EXPIRES,
        "draft_key": "draft:1234567890abcdef",
        "now": NOW,
    }
    values.update(overrides)
    return delivery.create_draft(**values)


def approval(**overrides) -> DeliveryApproval:
    values = {
        "report_hash": REPORT["report_hash"],
        "destination_id": "sim.review-room",
        "audience": tuple(REPORT["audience"]),
        "classification": "public",
        "source_rights_ref": "rights:synthetic-approved",
        "purpose": "report.review.simulation",
        "retention_days": 30,
        "redaction_profile": "exact_snapshot",
        "policy_decision_ref": "policy:synthetic-allow",
        "approval_ref": "approval:synthetic-exact",
        "approved_at": NOW,
        "expires_at": "2026-08-24T12:30:00Z",
        "destination_verified_until": "2026-08-24T12:30:00Z",
    }
    values.update(overrides)
    return DeliveryApproval(**values)


def activate(delivery: ReportDeliveryService, subscription_id: str, **overrides) -> dict:
    return delivery.activate(
        subscription_id,
        approval(**overrides),
        idempotency_key="activate:1234567890abcdef",
        now=NOW,
    )


def test_draft_is_disabled_and_exact_approval_creates_one_outbox(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    created = draft(delivery)
    assert created["status"] == "disabled"
    assert created["outbox"] is None
    assert created["external_delivery_authorized"] is False

    active = activate(delivery, created["subscription_id"])
    assert active["status"] == "active"
    assert active["outbox"]["status"] == "queued"
    assert active["outbox"]["attempts"] == 0
    assert [
        event["event_type"] for event in delivery.audit_events(created["subscription_id"])
    ] == [
        "subscription_drafted",
        "subscription_activated",
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_hash", "sha256:" + "f" * 64),
        ("destination_id", "sim.other-room"),
        ("audience", ("other audience",)),
        ("classification", "internal"),
        ("source_rights_ref", "rights:other-approved"),
        ("purpose", "other.report.purpose"),
        ("retention_days", 31),
        ("redaction_profile", "metadata_only"),
    ],
)
def test_activation_rejects_any_exact_approval_drift(
    tmp_path: Path, field: str, value: object
) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    with pytest.raises(ReportDeliveryDenied, match="exact delivery"):
        activate(delivery, subscription["subscription_id"], **{field: value})
    assert delivery.get_subscription(subscription["subscription_id"])["status"] == "disabled"


def test_policy_denial_expiry_destination_and_source_rights_fail_closed(tmp_path: Path) -> None:
    denied = service(tmp_path, allowed=False)
    subscription = draft(denied)
    with pytest.raises(ReportDeliveryDenied, match="CapAuth"):
        activate(denied, subscription["subscription_id"])
    assert (
        denied.audit_events(subscription["subscription_id"])[-1]["event_type"]
        == "activation_denied"
    )

    for changes in (
        {"expires_at": "2026-08-24T12:04:00Z"},
        {"destination_verified_until": "2026-08-24T12:04:00Z"},
        {"source_rights_state": "denied"},
        {"capability": "skdashboard.read"},
        {"simulation_only": False},
    ):
        delivery = service(tmp_path / str(len(changes)) / str(changes))
        item = draft(delivery)
        with pytest.raises(ReportDeliveryDenied):
            activate(delivery, item["subscription_id"], **changes)


def test_schedule_receipt_and_duplicate_processing_are_idempotent(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])
    destination = SimulationDestination("sim.review-room")
    assert delivery.dispatch_due(destination, now="2026-08-24T12:04:59Z") == []

    results = delivery.dispatch_due(destination, now=SCHEDULED)
    assert destination.calls == 1
    assert results[0]["status"] == "receipt_verified"
    receipt = results[0]["receipt"]
    assert receipt["report_hash"] == REPORT["report_hash"]
    assert receipt["simulation"] == 1
    assert receipt["retained_until"] == "2026-09-23T12:05:00Z"
    assert delivery.dispatch_due(destination, now="2026-08-24T12:06:00Z") == []
    assert destination.calls == 1

    repeated = delivery.activate(
        subscription["subscription_id"],
        approval(),
        idempotency_key="activate:1234567890abcdef",
        now=NOW,
    )
    assert repeated["receipt"] == receipt
    with pytest.raises(ReportDeliveryConflict):
        delivery.activate(
            subscription["subscription_id"],
            approval(),
            idempotency_key="activate:different-abcdef",
            now=NOW,
        )
    with pytest.raises(ReportDeliveryConflict):
        delivery.activate(
            subscription["subscription_id"],
            approval(approval_ref="approval:changed-exact"),
            idempotency_key="activate:1234567890abcdef",
            now=NOW,
        )


def test_destination_error_retries_then_verifies_receipt(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])
    destination = SimulationDestination("sim.review-room", ("error", "success"))

    first = delivery.dispatch_due(destination, now=SCHEDULED)[0]
    assert first["status"] == "active"
    assert first["outbox"]["status"] == "retry_wait"
    assert first["outbox"]["attempts"] == 1
    assert first["outbox"]["last_failure_code"] == "DESTINATION_ERROR"
    assert delivery.dispatch_due(destination, now="2026-08-24T12:05:59Z") == []
    second = delivery.dispatch_due(destination, now="2026-08-24T12:06:00Z")[0]
    assert second["status"] == "receipt_verified"
    assert second["outbox"]["attempts"] == 2
    assert destination.calls == 2


def test_destination_stops_after_bounded_retry_failure(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])
    destination = SimulationDestination("sim.review-room", ("error", "error", "error"))
    assert delivery.dispatch_due(destination, now=SCHEDULED)[0]["outbox"]["attempts"] == 1
    assert (
        delivery.dispatch_due(destination, now="2026-08-24T12:06:00Z")[0]["outbox"]["attempts"]
        == 2
    )
    failed = delivery.dispatch_due(destination, now="2026-08-24T12:08:00Z")[0]
    assert failed["status"] == "failed"
    assert failed["outbox"]["attempts"] == 3
    assert delivery.dispatch_due(destination, now="2026-08-24T12:20:00Z") == []
    assert destination.calls == 3


def test_interrupted_dispatch_fails_closed_without_silent_resend(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    active = activate(delivery, subscription["subscription_id"])
    with delivery._connect() as connection:
        connection.execute(
            """UPDATE outbox SET status='dispatching',claim_expires_at=?
            WHERE outbox_id=?""",
            ("2026-08-24T12:04:59Z", active["outbox"]["outbox_id"]),
        )
    destination = SimulationDestination("sim.review-room")
    result = delivery.dispatch_due(destination, now=SCHEDULED)[0]
    assert result["status"] == "failed"
    assert result["outbox"]["last_failure_code"] == "DELIVERY_OUTCOME_UNKNOWN"
    assert destination.calls == 0
    assert delivery.audit_events(subscription["subscription_id"])[-1]["event_type"] == (
        "delivery_outcome_unknown"
    )


def test_active_claim_is_not_recycled_or_cancelled(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    active = activate(delivery, subscription["subscription_id"])
    with delivery._connect() as connection:
        connection.execute(
            """UPDATE outbox SET status='dispatching',claim_expires_at=?
            WHERE outbox_id=?""",
            ("2026-08-24T12:05:30Z", active["outbox"]["outbox_id"]),
        )
    destination = SimulationDestination("sim.review-room")
    assert delivery.dispatch_due(destination, now=SCHEDULED) == []
    assert destination.calls == 0
    with pytest.raises(ReportDeliveryConflict, match="in-flight"):
        delivery.cancel(
            subscription["subscription_id"],
            cancellation_ref="cancel:review-owner",
            now=SCHEDULED,
        )


def test_policy_is_rechecked_immediately_before_dispatch(tmp_path: Path) -> None:
    current = {"allow": True}
    snapshots = ReportSnapshotStore(tmp_path)
    snapshots.put(REPORT)
    delivery = ReportDeliveryService(
        tmp_path,
        policy_checker=lambda _approval: current["allow"],
        snapshot_store=snapshots,
    )
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])
    current["allow"] = False
    destination = SimulationDestination("sim.review-room")
    result = delivery.dispatch_due(destination, now=SCHEDULED)[0]
    assert result["status"] == "failed"
    assert result["outbox"]["last_failure_code"] == "POLICY_OR_INTEGRITY_DENIED"
    assert destination.calls == 0


def test_database_symlink_and_loose_mode_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "report-delivery"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.sqlite3"
    target.write_bytes(b"")
    target.chmod(0o600)
    (root / "simulation.sqlite3").symlink_to(target)
    with pytest.raises(ReportDeliveryError, match="database is unsafe"):
        ReportDeliveryService(
            tmp_path,
            policy_checker=lambda _approval: True,
            snapshot_store=ReportSnapshotStore(tmp_path),
        )

    other = tmp_path / "other"
    root = other / "report-delivery"
    root.mkdir(parents=True, mode=0o700)
    database = root / "simulation.sqlite3"
    database.write_bytes(b"")
    database.chmod(0o644)
    with pytest.raises(ReportDeliveryError, match="database is unsafe"):
        ReportDeliveryService(
            other,
            policy_checker=lambda _approval: True,
            snapshot_store=ReportSnapshotStore(other),
        )


def test_expiry_and_cancellation_never_reach_destination(tmp_path: Path) -> None:
    expiring = service(tmp_path / "expiry")
    item = draft(expiring, scheduled_for="2026-08-24T12:01:00Z", expires_at="2026-08-24T12:02:00Z")
    expiring.activate(
        item["subscription_id"],
        approval(
            expires_at="2026-08-24T12:10:00Z",
            destination_verified_until="2026-08-24T12:10:00Z",
        ),
        idempotency_key="activate:expiry-123456",
        now=NOW,
    )
    destination = SimulationDestination("sim.review-room")
    assert expiring.dispatch_due(destination, now="2026-08-24T12:02:00Z")[0]["status"] == "expired"
    assert destination.calls == 0

    expired_before_activation = service(tmp_path / "activation-expiry")
    item = draft(
        expired_before_activation,
        scheduled_for="2026-08-24T12:01:00Z",
        expires_at="2026-08-24T12:02:00Z",
    )
    with pytest.raises(ReportDeliveryDenied, match="expired before activation"):
        expired_before_activation.activate(
            item["subscription_id"],
            approval(
                expires_at="2026-08-24T12:10:00Z",
                destination_verified_until="2026-08-24T12:10:00Z",
            ),
            idempotency_key="activate:expired-123456",
            now="2026-08-24T12:02:00Z",
        )

    cancelling = service(tmp_path / "cancel")
    item = draft(cancelling)
    activate(cancelling, item["subscription_id"])
    cancelled = cancelling.unsubscribe(
        item["subscription_id"],
        cancellation_ref="cancel:review-owner",
        now="2026-08-24T12:01:00Z",
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["outbox"]["status"] == "cancelled"
    assert cancelling.dispatch_due(destination, now=SCHEDULED) == []
    assert destination.calls == 0


def test_draft_rejects_sensitive_fields_scope_and_immutable_mismatch(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    with pytest.raises(ReportDeliveryError, match="sensitive"):
        draft(delivery, destination_id="https://example.test/export")
    with pytest.raises(ReportDeliveryError, match="audience"):
        draft(delivery, audience=("Bearer hidden",))
    with pytest.raises(ReportDeliveryError, match="classification"):
        draft(delivery, classification="internal")

    protected = dict(REPORT, scope={"matter_id": "protected"})

    class ProtectedStore:
        def get(self, _snapshot_id):
            return protected

    with pytest.raises(ReportDeliveryError):
        service(tmp_path / "protected", store=ProtectedStore()).create_draft(
            snapshot_id=REPORT["snapshot_id"],
            destination_id="sim.review-room",
            audience=tuple(REPORT["audience"]),
            classification="public",
            source_rights_ref="rights:synthetic-approved",
            purpose="report.review.simulation",
            retention_days=30,
            redaction_profile="exact_snapshot",
            scheduled_for=SCHEDULED,
            expires_at=EXPIRES,
            draft_key="draft:protected-1234",
            now=NOW,
        )


def test_equivalent_drafts_with_distinct_keys_remain_distinct(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    first = draft(delivery)
    second = draft(delivery, draft_key="draft:fedcba0987654321")
    assert first["subscription_id"] != second["subscription_id"]


def test_confidential_snapshot_requires_and_uses_metadata_redaction(tmp_path: Path) -> None:
    from skdashboard.control_plane_fixture import SCOPE, _metric
    from skdashboard.dashboard_reports import build_report_snapshot

    classified_metric = _metric()
    classified_metric["classification"] = {
        "level": "confidential",
        "purpose": "client qualification",
    }
    report = build_report_snapshot(
        report_type="daily_operations",
        audience=["public synthetic development"],
        generated_at="2026-08-24T12:00:30Z",
        as_of="2026-08-24T12:00:00Z",
        scope=SCOPE,
        baseline=None,
        sections=[
            {
                "section_id": "synthetic",
                "title": "Must not enter metadata-only payload",
                "metric_results": [classified_metric],
                "insights": [],
            }
        ],
    )
    snapshots = ReportSnapshotStore(tmp_path)
    snapshots.put(report)
    delivery = ReportDeliveryService(
        tmp_path,
        policy_checker=lambda _approval: True,
        snapshot_store=snapshots,
    )
    with pytest.raises(ReportDeliveryError, match="metadata-only"):
        draft(
            delivery,
            snapshot_id=report["snapshot_id"],
            classification="confidential",
        )
    item = draft(
        delivery,
        snapshot_id=report["snapshot_id"],
        classification="confidential",
        redaction_profile="metadata_only",
        draft_key="draft:confidential-meta",
    )
    with delivery._connect() as connection:
        row = connection.execute(
            "SELECT * FROM subscriptions WHERE subscription_id=?", (item["subscription_id"],)
        ).fetchone()
    payload = delivery._payload(row, report)
    assert b"Must not enter metadata-only payload" not in payload
    assert report["report_hash"].encode() in payload


def test_schedule_audit_receipt_and_database_exclude_content_and_secrets(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])
    delivery.dispatch_due(SimulationDestination("sim.review-room"), now=SCHEDULED)

    projected = delivery.get_subscription(subscription["subscription_id"])
    audit = delivery.audit_events(subscription["subscription_id"])
    serialized = str(projected) + str(audit)
    assert "Bearer" not in serialized
    assert "capability_token" not in serialized
    assert "secret" not in serialized.lower()
    assert REPORT["sections"][0]["title"] not in serialized
    assert REPORT["sections"][0]["metric_results"][0]["metric_id"] not in serialized
    database = delivery.database.read_bytes()
    assert REPORT["sections"][0]["title"].encode() not in database
    assert REPORT["sections"][0]["metric_results"][0]["metric_id"].encode() not in database


def test_no_http_or_non_simulation_destination_surface_is_added(tmp_path: Path) -> None:
    delivery = service(tmp_path)
    subscription = draft(delivery)
    activate(delivery, subscription["subscription_id"])

    class ExternalDestination:
        simulation_only = False

    with pytest.raises(ReportDeliveryDenied, match="built-in simulation"):
        delivery.dispatch_due(ExternalDestination(), now=SCHEDULED)

    from skdashboard.dashboard import create_app

    paths = {route.path for route in create_app(tmp_path).routes}
    assert not any(
        "subscription" in path or "delivery" in path or "export" in path for path in paths
    )
    assert DELIVERY_CAPABILITY not in str(paths)
