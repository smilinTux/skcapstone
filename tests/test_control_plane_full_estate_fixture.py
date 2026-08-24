from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker, RefResolver
from starlette.testclient import TestClient

from skdashboard.control_plane_adapters import SPECS, Reader, aggregate_reader, project_estate
from skdashboard.control_plane_metric_registry import REGISTRY, calculate_metric, registry_manifest
from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]
ESTATE_FIXTURE = ROOT / "tests/fixtures/control_plane_full_estate.v1.0.0.json"
METRIC_FIXTURE = ROOT / "tests/fixtures/control_plane_metric_calculations.v1.0.0.json"
NOW = datetime(2026, 8, 24, 12, 0, 30, tzinfo=timezone.utc)
PORTFOLIO_ID = "synthetic-estate"
CONTRACTS = ROOT / "docs/contracts/v1.1.0"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(value: object) -> str:
    encoded = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _readers(fixture: dict, *, observed_at: str | None = None) -> dict[str, Reader]:
    readers = {}
    for case in fixture["estate_cases"]:
        if failure := case.get("failure"):
            readers[case["adapter_id"]] = Reader(failure=failure)
            continue
        readers[case["adapter_id"]] = aggregate_reader(
            case["aggregate"],
            expected=case["coverage"]["expected"],
            reporting=case["coverage"]["reporting"],
            observed_at=observed_at or case.get("observed_at", fixture["observed_at"]),
            watermark_data=case["watermark"],
            errors=case["errors"],
            has_observations=case["has_observations"],
        )
    return readers


def _metric_observation(case: dict, fixture: dict) -> dict:
    evidence = case.get("has_evidence", True)
    scope = {"portfolio_id": PORTFOLIO_ID}
    if lane := case.get("measurement_lane"):
        scope["measurement_lane"] = lane
    return {
        "schema_version": fixture["schema_version"],
        "definition_version": case["definition_version"],
        "truth_state": case["truth_state"],
        "numerator": case["numerator"],
        "denominator": case["denominator"],
        "sample_size": case["sample_size"],
        "scope": scope,
        "window": fixture["window"],
        "visibility": case.get("visibility", {"state": "visible", "authorization": "authorized"}),
        "confidence": case.get("confidence"),
        "policy_decision_ref": case.get("policy_decision_ref"),
        "source": {
            "owner": case["owner"],
            "adapter_id": case["adapter_id"],
            "adapter_version": "1.0.0",
            "observed_at": "2026-08-24T11:59:00Z",
            "projected_at": "2026-08-24T12:00:00Z",
            "freshness_ttl_seconds": 300,
            "watermarks": (
                [{"source": case["adapter_id"], "value": "synthetic-r1"}] if evidence else []
            ),
            "evidence_refs": ([f"evidence:{case['metric_id']}:synthetic-r1"] if evidence else []),
        },
        "data_quality": {
            "coverage_numerator": case["coverage"][0],
            "coverage_denominator": case["coverage"][1],
            "errors": case.get("errors", []),
            "exclusions": case["exclusions"],
            "notes": ["synthetic fixture only"],
        },
    }


def _metric_results(estate_fixture: dict) -> list[dict]:
    fixture = _load(METRIC_FIXTURE)
    cases = json.loads(json.dumps(fixture["cases"]))
    for raw_index, override in estate_fixture["metric_pack"]["case_overrides"].items():
        cases[int(raw_index)].update(override)
    return [
        calculate_metric(case["metric_id"], _metric_observation(case, fixture)) for case in cases
    ]


def _report_snapshot(fixture: dict, estate: list[dict], metrics: list[dict]) -> dict:
    watermarks = sorted(
        (
            {"source": item["adapter_id"], "value": item["watermark"]["value"]}
            for item in estate
            if item["aggregate"] is not None
            and item["visibility"] == {"state": "visible", "authorization": "authorized"}
            and item["watermark"]["value"]
        ),
        key=lambda item: item["source"],
    )
    errors = sorted(
        f"{item['adapter_id']}: {error['code']}" for item in estate for error in item["errors"]
    )
    snapshot = {
        "snapshot_id": "rpt-synthetic-estate-v1",
        "schema_version": fixture["schema_version"],
        "report_type": fixture["report_profile"]["report_type"],
        "audience": ["public synthetic qualification"],
        "generated_at": fixture["projected_at"],
        "as_of": fixture["observed_at"],
        "scope": {"portfolio_id": fixture["portfolio_id"]},
        "baseline": "previous",
        "metric_definition_hashes": registry_manifest()["definition_hashes"],
        "source_watermarks": watermarks,
        "quality_statement": {
            "truth_state": "partial",
            "visibility": {"state": "visible", "authorization": "authorized"},
            "summary": "Public synthetic estate contains deliberate degraded conditions.",
            "errors": errors,
            "exclusions": ["protected detail and production state"],
        },
        "sections": [
            {
                "section_id": "full_estate",
                "title": "Public synthetic full estate",
                "metric_results": metrics,
                "insights": [],
            }
        ],
        "review_state": {"state": "unreviewed"},
    }
    snapshot["report_hash"] = _hash(snapshot)
    return snapshot


def _validate_report(snapshot: dict) -> None:
    schema_path = CONTRACTS / "control-plane-report-snapshot.v1.1.0.schema.json"
    schema = _load(schema_path)
    store = {path.as_uri(): _load(path) for path in CONTRACTS.glob("*.json")}
    resolver = RefResolver(
        base_uri=CONTRACTS.as_uri() + "/",
        referrer=schema,
        store=store,
    )
    Draft202012Validator(
        schema,
        resolver=resolver,
        format_checker=FormatChecker(),
    ).validate(snapshot)


def _ui_rows(fixture: dict, estate: list[dict]) -> list[dict]:
    projected = {item["adapter_id"]: item for item in estate}
    return [
        {
            "silo": case["silo"],
            "signal": case["signal"],
            "truth_state": projected[case["adapter_id"]]["truth_state"],
            "watermark": projected[case["adapter_id"]]["watermark"]["value"],
            "errors": projected[case["adapter_id"]]["errors"],
        }
        for case in fixture["estate_cases"]
    ]


def _portable_payload(fixture: dict) -> dict:
    return {
        "fixture_version": fixture["fixture_version"],
        "schema_version": fixture["schema_version"],
        "classification": fixture["classification"],
        "synthetic": fixture["synthetic"],
        "portfolio_id": fixture["portfolio_id"],
        "estate_cases": fixture["estate_cases"],
        "metric_conditions": fixture["metric_conditions"],
    }


def test_fixture_spans_every_public_synthetic_estate_signal_and_truth_condition() -> None:
    fixture = _load(ESTATE_FIXTURE)
    cases = fixture["estate_cases"]
    expected_silos = {
        "portfolio",
        "flow",
        "itil_sre",
        "dora",
        "architecture",
        "fleet",
        "ai",
        "economy",
        "governance",
        "legal_program",
        "corpus_pipeline",
        "atlas",
        "skos",
    }

    assert hashlib.sha256(ESTATE_FIXTURE.read_bytes()).hexdigest() == (
        "926f0374b8f32e64d0f370f9a6197b848a95a3403b8d497c5c67848e77fb821d"
    )
    assert fixture["classification"] == "public"
    assert fixture["synthetic"] is True
    assert {case["adapter_id"] for case in cases} == {spec.adapter_id for spec in SPECS}
    assert {case["silo"] for case in cases} == expected_silos
    assert set(fixture["report_profile"]["included_silos"]) == expected_silos
    assert set(fixture["metric_conditions"]) == {
        "healthy",
        "observed_zero",
        "stale",
        "partial",
        "unavailable",
        "unknown",
        "not_applicable",
        "conflict",
        "forecast",
        "mixed_lane",
    }

    estate = project_estate(_readers(fixture), now=NOW)
    by_id = {item["adapter_id"]: item for item in estate}
    for case in cases:
        assert by_id[case["adapter_id"]]["truth_state"] == case["expected_truth_state"]

    assert by_id["skcapstone.itil"]["aggregate"]["open_incidents"] == 0
    assert by_id["skcapstone.itil"]["watermark"]["value"]
    assert by_id["cmdb.configuration"]["truth_state"] == "partial"
    assert by_id["cmdb.configuration"]["errors"][0]["code"] == "SOURCE_PARTIAL"
    assert fixture["metric_conditions"]["conflict"] == "architecture.drift_signals@4"
    conflict = next(case for case in cases if case["adapter_id"] == "cmdb.configuration")
    assert conflict["condition"] == "conflict"
    assert any("conflict" in error for error in conflict["errors"])
    assert by_id["skcapstone.fleet"]["aggregate"] is None
    assert by_id["skcapstone.fleet"]["errors"][0]["code"] == "SOURCE_TIMEOUT"


def test_metric_pack_hashes_results_definitions_forecast_and_lanes_deterministically() -> None:
    estate_fixture = _load(ESTATE_FIXTURE)
    metric_fixture = _load(METRIC_FIXTURE)
    results = _metric_results(estate_fixture)

    assert (
        hashlib.sha256(METRIC_FIXTURE.read_bytes()).hexdigest()
        == (estate_fixture["metric_pack"]["calculation_fixture_sha256"])
    )
    assert registry_manifest()["registry_hash"] == estate_fixture["metric_pack"]["registry_hash"]
    assert _hash(results) == estate_fixture["metric_pack"]["result_set_sha256"]
    for case, result in zip(metric_fixture["cases"], results, strict=True):
        definition = REGISTRY[(case["metric_id"], case["definition_version"])]
        assert result["calculation"]["definition_hash"] == definition.definition_hash
        assert result["value"] == case["expected"]
        assert result["scope"]["portfolio_id"] == estate_fixture["portfolio_id"]

    def located(locator: str) -> dict:
        metric_id, raw_index = locator.rsplit("@", 1)
        result = results[int(raw_index)]
        assert result["metric_id"] == metric_id
        return result

    conditions = estate_fixture["metric_conditions"]
    assert located(conditions["healthy"])["truth_state"] == "current"
    zero = located(conditions["observed_zero"])
    assert zero["value"] == 0
    assert zero["source"]["evidence_refs"] and zero["source"]["watermarks"]
    for condition in ("stale", "partial", "unavailable", "unknown", "not_applicable"):
        assert located(conditions[condition])["truth_state"] == condition
    assert located(conditions["not_applicable"])["data_quality"]["exclusions"]
    conflict = located(conditions["conflict"])
    assert conflict["truth_state"] == "partial"
    assert conflict["data_quality"]["errors"] == ["declared and observed revisions conflict"]

    forecast = next(result for result in results if result["measurement_kind"] == "forecast")
    assert forecast["confidence"]["lower"] <= forecast["confidence"]["upper"]
    lane_results = [result for result in results if "measurement_lane" in result["scope"]]
    assert {result["scope"]["measurement_lane"] for result in lane_results} == {
        "harness_reported",
        "gateway_observed",
    }
    assert [located(locator) for locator in conditions["mixed_lane"]] == lane_results[:2]
    assert _hash(list(reversed(results))) != estate_fixture["metric_pack"]["result_set_sha256"]


def test_one_pack_drives_api_report_forecast_failure_agent_and_mcp_qualification() -> None:
    fixture = _load(ESTATE_FIXTURE)
    readers = _readers(fixture)
    estate = project_estate(readers, now=NOW)
    metrics = _metric_results(fixture)
    report = _report_snapshot(fixture, estate, metrics)
    profiles = fixture["qualification_profiles"]

    assert set(fixture["qualification_targets"]) == {
        "ui",
        "api",
        "agent",
        "mcp",
        "report",
        "forecast",
        "failure",
    }
    assert set(profiles) == set(fixture["qualification_targets"])
    assert report["report_hash"] == fixture["report_profile"]["expected_report_hash"]
    assert len(report["source_watermarks"]) == 12
    assert all(item["value"].startswith("sha256:") for item in report["source_watermarks"])
    assert len(report["sections"]) == profiles["report"]["expected_sections"]
    _validate_report(report)

    rows = _ui_rows(fixture, estate)
    assert len(rows) == profiles["ui"]["expected_item_count"]
    assert all(set(row) == set(profiles["ui"]["required_fields"]) for row in rows)
    assert _hash(rows) == profiles["ui"]["expected_row_set_sha256"]

    portable = _portable_payload(fixture)
    assert profiles["agent"]["access"] == profiles["mcp"]["access"] == "read_only"
    assert _hash(portable) == profiles["agent"]["expected_payload_sha256"]
    assert _hash(portable) == profiles["mcp"]["expected_payload_sha256"]
    assert json.loads(json.dumps(portable, sort_keys=True)) == portable

    runtime_readers = _readers(fixture, observed_at=datetime.now(timezone.utc).isoformat())
    app = create_app(
        Path("/tmp/public-synthetic-estate"),
        control_plane_authorizer=lambda bearer, permission, _target: (
            bearer == "fixture-read" and permission == "skdashboard.read"
        ),
    )
    with patch("skdashboard.control_plane_adapters.default_readers", return_value=runtime_readers):
        client = TestClient(app)
        assert client.get(profiles["api"]["route"]).status_code == 401
        response = client.get(
            profiles["api"]["route"],
            headers={
                "Authorization": "Bearer fixture-read",
                "Origin": "https://10.0.0.139:7778",
            },
        )
    assert response.status_code == profiles["api"]["expected_status"]
    assert set(profiles["api"]["required_envelope_fields"]) <= set(response.json())
    api_items = response.json()["items"]
    assert {item["adapter_id"] for item in api_items if "adapter_id" in item} == {
        spec.adapter_id for spec in SPECS
    }
    assert any(item.get("projection_type") == "data_quality" for item in api_items)
    result_states = {result["truth_state"] for result in metrics}
    assert set(profiles["failure"]["required_truth_states"]) <= result_states
    forecast = next(
        result for result in metrics if result["metric_id"] == profiles["forecast"]["metric_id"]
    )
    assert forecast["measurement_kind"] == "forecast"
    assert forecast["confidence"]["lower"] <= forecast["confidence"]["upper"]

    serialized = json.dumps(fixture, sort_keys=True).lower()
    forbidden = (
        "prompt",
        "response",
        "session",
        "credential",
        "capability",
        "source_path",
        "tenant_id",
        "matter_id",
        "inbox",
    )
    assert not any(term in serialized for term in forbidden)
    assert _load(ESTATE_FIXTURE) == fixture


def test_conflict_and_report_watermark_checks_are_sensitive() -> None:
    fixture = _load(ESTATE_FIXTURE)
    readers = _readers(fixture)
    estate = project_estate(readers, now=NOW)
    conflict = next(case for case in fixture["estate_cases"] if case["condition"] == "conflict")

    resolved = json.loads(json.dumps(conflict))
    resolved["errors"] = []
    resolved["coverage"]["reporting"] = resolved["coverage"]["expected"]
    resolved_fixture = json.loads(json.dumps(fixture))
    resolved_fixture["estate_cases"] = [
        resolved if case["adapter_id"] == resolved["adapter_id"] else case
        for case in resolved_fixture["estate_cases"]
    ]
    resolved_item = {
        item["adapter_id"]: item for item in project_estate(_readers(resolved_fixture), now=NOW)
    }[resolved["adapter_id"]]
    assert resolved_item["truth_state"] == "current"

    report = _report_snapshot(fixture, estate, _metric_results(fixture))
    changed_estate = json.loads(json.dumps(estate))
    changed = next(item for item in changed_estate if item["watermark"]["value"])
    changed["watermark"]["value"] += "-changed"
    changed_report = _report_snapshot(fixture, changed_estate, _metric_results(fixture))
    assert changed_report["report_hash"] != report["report_hash"]

    resolved_metric_fixture = json.loads(json.dumps(fixture))
    resolved_metric_fixture["metric_pack"]["case_overrides"]["4"] = {
        "truth_state": "current",
        "errors": [],
        "coverage": [12, 12],
    }
    resolved_metrics = _metric_results(resolved_metric_fixture)
    assert resolved_metrics[4]["truth_state"] == "current"
    assert resolved_metrics[4]["data_quality"]["errors"] == []
    assert _hash(resolved_metrics) != _hash(_metric_results(fixture))
