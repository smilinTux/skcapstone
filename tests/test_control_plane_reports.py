from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, RefResolver

from skdashboard.dashboard_reports import (
    ReportSnapshotError,
    ReportSnapshotStore,
    build_report_snapshot,
    compare_report_snapshots,
    report_hash,
    validate_report_snapshot,
)

ROOT = Path(__file__).parents[1]
CONTRACTS = ROOT / "docs/contracts/v1.1.0"


def metric(*, value=4, truth="current", kind="derived", definition_hash=None):
    return {
        "metric_id": "flow.review_coverage",
        "schema_version": "1.1.0",
        "definition_version": "1.0.0",
        "label": "Review coverage",
        "value": value,
        "unit": "percent",
        "polarity": "higher_is_better",
        "numerator": value if value is not None else None,
        "denominator": 100 if value is not None else None,
        "sample_size": 10 if value is not None else None,
        "scope": {"portfolio_id": "estate"},
        "grain": "estate",
        "window": {
            "start": "2026-08-17T00:00:00Z",
            "end": "2026-08-24T00:00:00Z",
            "timezone": "UTC",
            "baseline": "previous",
        },
        "target": None,
        "truth_state": truth,
        "visibility": {"state": "visible", "authorization": "authorized"},
        "measurement_kind": kind,
        "confidence": (
            {"level": 0.8, "lower": 2, "upper": 7, "method": "fixture"}
            if kind in {"estimated", "forecast"}
            else None
        ),
        "source": {
            "owner": "skcoord",
            "adapter_id": "skcoord.flow",
            "adapter_version": "1.0.0",
            "observed_at": "2026-08-24T00:00:00Z",
            "projected_at": "2026-08-24T00:01:00Z",
            "freshness_ttl_seconds": 300,
            "watermarks": [{"source": "skcoord.flow", "value": "sha256:" + "a" * 64}],
            "evidence_refs": ["evidence:flow"],
        },
        "data_quality": {
            "coverage_numerator": 10,
            "coverage_denominator": 10,
            "errors": [] if truth == "current" else [f"source is {truth}"],
            "exclusions": ["individual activity"],
            "notes": [],
        },
        "calculation": {
            "definition_hash": definition_hash or "sha256:" + "b" * 64,
            "method": "ratio_percent",
            "expression": "100 * numerator / denominator",
            "calculation_ref": "registry:1.0.0:flow.review_coverage@1.0.0",
        },
        "classification": {
            "level": "internal",
            "policy_decision_ref": None,
            "purpose": "control_plane_reporting",
        },
    }


def snapshot(*, value=4, truth="current", kind="derived", supersedes=None, definition_hash=None):
    return build_report_snapshot(
        report_type="weekly_portfolio",
        audience=["portfolio review"],
        generated_at="2026-08-24T00:02:00Z" if supersedes is None else "2026-08-24T00:03:00Z",
        as_of="2026-08-24T00:00:00Z",
        scope={"portfolio_id": "estate"},
        baseline="previous",
        sections=[
            {
                "section_id": "portfolio",
                "title": "Portfolio",
                "metric_results": [
                    metric(
                        value=value,
                        truth=truth,
                        kind=kind,
                        definition_hash=definition_hash,
                    )
                ],
                "insights": [],
            }
        ],
        supersedes=supersedes,
    )


def validate_schema(value):
    schema_path = CONTRACTS / "control-plane-report-snapshot.v1.1.0.schema.json"
    schema = json.loads(schema_path.read_text())
    store = {path.as_uri(): json.loads(path.read_text()) for path in CONTRACTS.glob("*.json")}
    Draft202012Validator(
        schema,
        resolver=RefResolver(
            base_uri=CONTRACTS.as_uri() + "/",
            referrer=schema,
            store=store,
        ),
        format_checker=FormatChecker(),
    ).validate(value)


def test_builder_is_schema_valid_content_hashed_and_reproducible():
    first = snapshot(kind="forecast")
    second = snapshot(kind="forecast")
    validate_schema(first)
    assert first == second
    assert first["report_hash"] == report_hash(first)
    assert first["metric_definition_hashes"] == {
        "flow.review_coverage@1.0.0": "sha256:" + "b" * 64
    }
    assert first["source_watermarks"] == [
        {"source": "skcoord.flow", "value": "sha256:" + "a" * 64}
    ]
    quality = first["quality_statement"]
    assert quality["truth_state"] == "current"
    assert "1 forecast" in quality["summary"]
    assert "1 current" in quality["summary"]

    changed = snapshot(value=5, kind="forecast")
    assert changed["snapshot_id"] != first["snapshot_id"]
    assert changed["report_hash"] != first["report_hash"]


def test_store_is_write_once_idempotent_and_supersession_preserves_prior(tmp_path: Path):
    store = ReportSnapshotStore(tmp_path)
    original = snapshot()
    correction = snapshot(value=5, supersedes=original["snapshot_id"])
    assert store.put(original) == original
    assert store.put(original) == original
    assert store.put(correction) == correction
    assert store.get(original["snapshot_id"]) == original
    assert store.get(correction["snapshot_id"])["supersedes"] == original["snapshot_id"]
    assert len(store.list()) == 2

    path = tmp_path / "reports/snapshots" / f"{original['snapshot_id']}.json"
    tampered = dict(original, audience=["changed"])
    path.chmod(0o600)
    path.write_text(json.dumps(tampered))
    with pytest.raises(ReportSnapshotError, match="hash"):
        store.get(original["snapshot_id"])


def test_store_rejects_missing_superseded_snapshot_and_protected_scope(tmp_path: Path):
    report = snapshot(supersedes="rpt-does-not-exist")
    with pytest.raises(KeyError):
        ReportSnapshotStore(tmp_path).put(report)
    with pytest.raises(ReportSnapshotError, match="protected"):
        build_report_snapshot(
            report_type="ad_hoc_evidence",
            audience=["review"],
            generated_at="2026-08-24T00:00:00Z",
            as_of="2026-08-24T00:00:00Z",
            scope={"matter_id": "protected"},
            baseline=None,
            sections=[
                {
                    "section_id": "evidence",
                    "title": "Evidence",
                    "metric_results": [metric()],
                    "insights": [],
                }
            ],
        )


def test_comparison_preserves_truth_and_definition_incompatibility():
    prior = snapshot(value=4)
    current = snapshot(value=7, supersedes=prior["snapshot_id"])
    comparison = compare_report_snapshots(current, prior)
    assert comparison["state"] == "comparable"
    assert comparison["metric_changes"][0]["delta"] == 3

    unavailable = snapshot(
        value=None,
        truth="unavailable",
        supersedes=prior["snapshot_id"],
    )
    unavailable_change = compare_report_snapshots(unavailable, prior)["metric_changes"][0]
    assert unavailable_change["comparable"] is False
    assert unavailable_change["delta"] is None
    assert unavailable_change["current_truth_state"] == "unavailable"

    changed_definition = snapshot(
        value=7,
        supersedes=prior["snapshot_id"],
        definition_hash="sha256:" + "c" * 64,
    )
    definition_change = compare_report_snapshots(changed_definition, prior)["metric_changes"][0]
    assert definition_change["definition_changed"] is True
    assert definition_change["comparable"] is False


def test_validation_rejects_frozen_metric_or_watermark_tampering():
    report = snapshot()
    report["sections"][0]["metric_results"][0]["value"] = 99
    report["report_hash"] = report_hash(report)
    with pytest.raises(ReportSnapshotError, match="content addressed|calculation"):
        validate_report_snapshot(report)


def test_validation_rejects_protected_scope_and_model_provenance_tampering():
    report = snapshot()
    report["scope"] = {"tenant_id": "protected"}
    report["report_hash"] = report_hash(report)
    with pytest.raises(ReportSnapshotError, match="protected"):
        validate_report_snapshot(report)

    with_insight = snapshot()
    with_insight["model_provenance"] = [
        {
            "logical_route": "unbound",
            "transport_profile": "fixture",
            "gateway_revision": "fixture",
            "backend": "fixture",
            "requested_model": "fixture",
            "served_model": "fixture",
            "model_revision": "fixture",
            "prompt_hash": "sha256:" + "c" * 64,
            "schema_hash": "sha256:" + "d" * 64,
        }
    ]
    with_insight["report_hash"] = report_hash(with_insight)
    with pytest.raises(ReportSnapshotError, match="model provenance|content addressed"):
        validate_report_snapshot(with_insight)
