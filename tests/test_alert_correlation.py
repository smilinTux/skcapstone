from datetime import datetime, timezone

from skcapstone.fleet.alert_correlation import (
    fault_signature, normalize_error, reconcile, severity,
)


def test_signature_strips_host_volatile_values():
    a = "2026-08-30T10:00:00Z pid=41 rotation failed card:510367e5"
    b = "2026-08-30T10:04:00Z pid=99 rotation failed card:abcdef12"
    assert normalize_error(a) == normalize_error(b)
    assert fault_signature("skfleet-rotate.service", a) == fault_signature("skfleet-rotate.service", b)


def test_severity_tracks_blast_radius():
    assert severity(1) == "warning"
    assert severity(2) == "high"
    assert severity(5) == "critical"


def test_reconcile_keeps_earliest_and_points_duplicate():
    now = datetime(2026, 8, 30, 0, 10, tzinfo=timezone.utc)
    records = [
        {"id": "late", "signature": "s", "state": "open", "created_at": "2026-08-30T00:01:00+00:00"},
        {"id": "early", "signature": "s", "state": "open", "created_at": "2026-08-30T00:00:00+00:00"},
    ]
    assert reconcile(records, now, 900) == [{"type": "incident_reconciled", "incident_id": "late", "state": "closed", "merged_into": "early", "signature": "s"}]
