"""Adapters report WHEN their source data was true (``source_observed_at``).

``observed_at`` on a normalized condition is the probe or pass clock and is never
evidence. Evidence freshness (operator_seat.evidence) rests on the source time
only, and an observation with no source time cannot be actuated on.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import (
    adapter,
    cmdb_adapter,
    fleet_adapter,
    skchat_adapter,
    skcode_adapter,
    skcomms_adapter,
    skdashboard_adapter,
    skgateway_adapter,
    skmemory_adapter,
    skos_adapter,
)

_SCHEMA = adapter.condition_schema(["Ready"])


def _normalize(payload):
    return adapter.normalize_observe("demo", payload, _SCHEMA, observed_at="2030-01-01T00:00:00Z")[
        "conditions"
    ][0]


def _seconds_ago(stamp: str) -> float:
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - parsed).total_seconds()


# --------------------------------------------------------------------------
# envelope
# --------------------------------------------------------------------------


def test_envelope_never_invents_a_source_time():
    cond = _normalize({"conditions": [{"type": "Ready", "status": "False"}]})
    assert "source_observed_at" not in cond
    assert cond["observed_at"] == "2030-01-01T00:00:00Z"  # probe clock, untouched


def test_envelope_keeps_a_per_condition_source_time():
    cond = _normalize(
        {
            "conditions": [
                {"type": "Ready", "status": "False", "source_observed_at": "2026-08-20T00:00:00Z"}
            ]
        }
    )
    assert cond["source_observed_at"] == "2026-08-20T00:00:00Z"


def test_envelope_applies_a_payload_level_source_time_to_conditions_without_their_own():
    cond = _normalize(
        {
            "source_observed_at": "2026-08-20T00:00:00Z",
            "conditions": [{"type": "Ready", "status": "False"}],
        }
    )
    assert cond["source_observed_at"] == "2026-08-20T00:00:00Z"


def test_a_condition_level_source_time_beats_the_payload_level_one():
    cond = _normalize(
        {
            "source_observed_at": "2026-08-20T00:00:00Z",
            "conditions": [
                {"type": "Ready", "status": "False", "source_observed_at": "2026-08-19T00:00:00Z"}
            ],
        }
    )
    assert cond["source_observed_at"] == "2026-08-19T00:00:00Z"


@pytest.mark.parametrize("bad", ["yesterday", "2026-08-20T00:00:00", "", 12345, None, True, []])
def test_envelope_drops_an_unusable_source_time(bad):
    cond = _normalize(
        {"conditions": [{"type": "Ready", "status": "False", "source_observed_at": bad}]}
    )
    assert "source_observed_at" not in cond


def test_an_unknown_condition_carries_no_source_time():
    out = adapter.normalize_observe(
        "demo", {"source_observed_at": "2026-08-20T00:00:00Z", "conditions": []}, _SCHEMA
    )
    assert out["conditions"][0]["status"] == "Unknown"
    assert "source_observed_at" not in out["conditions"][0]


# --------------------------------------------------------------------------
# live probes report the time they probed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module, inner",
    [
        (skchat_adapter, "skchat_observe"),
        (skcode_adapter, "skcode_observe"),
        (skcomms_adapter, "skcomms_observe"),
        (skdashboard_adapter, "skdashboard_observe"),
        (skgateway_adapter, "skgateway_observe"),
        (skmemory_adapter, "skmemory_observe"),
    ],
)
def test_live_probe_adapters_stamp_the_real_probe_time(monkeypatch, module, inner):
    monkeypatch.setattr(
        module, inner, lambda *a, **k: {"conditions": [{"type": "X", "status": "True"}]}
    )
    payload = module.observe(None, "1999-01-01T00:00:00Z")  # a bogus pass clock is ignored
    assert 0 <= _seconds_ago(payload["source_observed_at"]) < 5
    assert payload["conditions"] == [{"type": "X", "status": "True"}]


# --------------------------------------------------------------------------
# derived / cached sources report the time of the data underneath
# --------------------------------------------------------------------------


def test_cmdb_reports_the_artifact_time_not_the_probe_time(monkeypatch):
    monkeypatch.setattr(
        cmdb_adapter,
        "_verified_latest_artifact",
        lambda home: {"ended_at": "2026-08-01T00:00:00Z", "completeness": {"complete": True}},
    )
    out = cmdb_adapter.observe(None, "2026-08-01T00:10:00Z", manager_factory=_NoFindings)
    by_type = {c["type"]: c for c in out["conditions"]}
    assert by_type["CmdbReconcileFresh"]["source_observed_at"] == "2026-08-01T00:00:00Z"
    assert by_type["CmdbLastScanComplete"]["source_observed_at"] == "2026-08-01T00:00:00Z"
    # The relationship audit runs live, now.
    assert 0 <= _seconds_ago(by_type["CmdbAuditClean"]["source_observed_at"]) < 5


def test_cmdb_without_an_artifact_has_no_source_time_for_artifact_conditions(monkeypatch):
    monkeypatch.setattr(cmdb_adapter, "_verified_latest_artifact", lambda home: None)
    out = cmdb_adapter.observe(None, None, manager_factory=_NoFindings)
    by_type = {c["type"]: c for c in out["conditions"]}
    assert "source_observed_at" not in by_type["CmdbReconcileFresh"]
    assert "source_observed_at" not in by_type["CmdbLastScanComplete"]


class _NoFindings:
    def __init__(self, home):
        pass

    def audit_relationships(self):
        return []


def test_fleet_never_reports_the_pass_clock_as_a_source_time(tmp_path):
    # Card 21b01d01: the pass clock is the evaluation instant, not when the source data
    # was true. Each fleet condition carries its own status write time instead (see
    # test_fleet_actuation_target.py); the payload carries none.
    paths = FleetPaths(root=tmp_path / "fleet")
    out = fleet_adapter.fleet_observe(paths, "2026-08-20T00:00:00Z")
    assert "source_observed_at" not in out


def test_skos_backlog_reports_the_digest_time_and_live_probes_report_now(monkeypatch):
    monkeypatch.setattr(
        skos_adapter,
        "_default_probe",
        lambda digest=None: {
            "scheduler_alive": True,
            "gtd_draining": True,
            "digest_fresh": True,
            "grading_backlog": True,
        },
    )
    monkeypatch.setattr(
        skos_adapter, "_read_digest", lambda: {"window": {"to": "2026-08-01T00:00:00Z"}}
    )
    out = skos_adapter.observe()
    by_type = {c["type"]: c for c in out["conditions"]}
    assert by_type["GradingBacklog"]["source_observed_at"] == "2026-08-01T00:00:00Z"
    for live in ("SchedulerAlive", "GtdSinkDraining", "WatchdogDigestFresh"):
        assert 0 <= _seconds_ago(by_type[live]["source_observed_at"]) < 5


def test_skos_backlog_with_no_digest_time_has_no_source_time(monkeypatch):
    monkeypatch.setattr(
        skos_adapter,
        "_default_probe",
        lambda digest=None: {
            "scheduler_alive": True,
            "gtd_draining": True,
            "digest_fresh": None,
            "grading_backlog": None,
        },
    )
    monkeypatch.setattr(skos_adapter, "_read_digest", lambda: None)
    monkeypatch.setattr(skos_adapter, "_digest_path", lambda: None)
    out = skos_adapter.observe()
    by_type = {c["type"]: c for c in out["conditions"]}
    assert "source_observed_at" not in by_type["GradingBacklog"]


def test_skos_reads_the_digest_once_so_the_backlog_and_its_time_come_from_one_digest(
    monkeypatch,
):
    """G: two reads could pair a backlog from one digest with the time of a newer one."""
    import sys
    import types

    monkeypatch.setitem(
        sys.modules,
        "skos.operator_probe",
        types.SimpleNamespace(observe=lambda: {"conditions": []}),
    )
    reads = []

    def read_digest():
        reads.append(1)
        return {"window": {"to": f"2026-08-0{len(reads)}T00:00:00Z"}, "problems": []}

    monkeypatch.setattr(skos_adapter, "_read_digest", read_digest)
    out = skos_adapter.observe()
    assert len(reads) == 1
    by_type = {c["type"]: c for c in out["conditions"]}
    assert by_type["GradingBacklog"]["source_observed_at"] == "2026-08-01T00:00:00Z"
