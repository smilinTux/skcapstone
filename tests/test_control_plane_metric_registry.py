from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from skdashboard.control_plane_metric_registry import (
    APPROVED_FAMILIES,
    DEFINITIONS,
    MEASUREMENT_KINDS,
    REGISTRY,
    SCHEMA_VERSION,
    TRUTH_STATES,
    MetricContractError,
    _registry,
    calculate_metric,
    registry_manifest,
)

ROOT = Path(__file__).parents[1]
SCHEMA_PATH = ROOT / "docs/contracts/v1.1.0/control-plane-metric-result.v1.1.0.schema.json"
FIXTURE_PATH = ROOT / "tests/fixtures/control_plane_metric_calculations.v1.0.0.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _observation(case: dict, fixture: dict) -> dict:
    evidence = case.get("has_evidence", True)
    scope = {"portfolio_id": case.get("portfolio_id", "sk-estate")}
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
        "visibility": case.get(
            "visibility", {"state": "visible", "authorization": "authorized"}
        ),
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
                [{"source": case["adapter_id"], "value": "synthetic-r1"}]
                if evidence
                else []
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


def test_every_registered_family_emits_against_the_exact_frozen_schema() -> None:
    schema_bytes = SCHEMA_PATH.read_bytes()
    assert hashlib.sha256(schema_bytes).hexdigest() == (
        "0000825f24a702766f24ac47cc1207339d5a9bfd09d5719b933331b2d1336326"
    )
    validator = Draft202012Validator(json.loads(schema_bytes), format_checker=FormatChecker())
    fixture = _fixture()
    results = [
        calculate_metric(case["metric_id"], _observation(case, fixture))
        for case in fixture["cases"]
    ]
    for result in results:
        validator.validate(result)

    families = {
        REGISTRY[(result["metric_id"], result["definition_version"])].family
        for result in results
    }
    assert families == set(APPROVED_FAMILIES)
    assert {result["truth_state"] for result in results} == set(TRUTH_STATES)
    assert {result["measurement_kind"] for result in results} == set(MEASUREMENT_KINDS)


def test_golden_calculations_reproduce_inputs_exclusions_and_results() -> None:
    fixture = _fixture()
    for case in fixture["cases"]:
        observation = _observation(case, fixture)
        first = calculate_metric(case["metric_id"], observation)
        second = calculate_metric(case["metric_id"], json.loads(json.dumps(observation)))
        assert first == second
        assert first["numerator"] == case["numerator"]
        assert first["denominator"] == case["denominator"]
        assert first["sample_size"] == case["sample_size"]
        assert first["data_quality"]["exclusions"] == case["exclusions"]
        assert first["value"] == case["expected"]

    observed_zero = next(case for case in fixture["cases"] if case["expected"] == 0)
    zero = calculate_metric(observed_zero["metric_id"], _observation(observed_zero, fixture))
    assert zero["value"] == 0
    assert zero["source"]["evidence_refs"]
    assert zero["source"]["watermarks"]


def test_registry_is_versioned_hash_addressed_and_semantically_sensitive() -> None:
    assert len(REGISTRY) == len(DEFINITIONS)
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == (
        "352c55134ad6327aabd7bab132cb0f19ef327be53bcf3d2c3b28f6c6bee03b49"
    )
    manifest = registry_manifest()
    assert manifest["registry_version"] == "1.0.0"
    assert manifest["metric_result_schema_version"] == SCHEMA_VERSION
    assert manifest == registry_manifest()
    assert manifest["registry_hash"] == (
        "sha256:198f5cfb5b42a67e52cdb00f17f8620fdc1dd4f3e28fb7bc9eb1f893e5baacfb"
    )
    assert set(manifest["definition_hashes"]) == {
        f"{definition.metric_id}@{definition.definition_version}" for definition in DEFINITIONS
    }

    definition = DEFINITIONS[0]
    assert replace(definition, label="Changed").definition_hash != definition.definition_hash
    duplicate = DEFINITIONS + (definition,)
    with pytest.raises(MetricContractError, match="duplicate metric definition"):
        _registry(duplicate)


def test_unknown_versions_missing_provenance_and_missing_values_fail_closed() -> None:
    fixture = _fixture()
    case = fixture["cases"][0]
    valid = _observation(case, fixture)
    mutations = []

    unknown_schema = dict(valid, schema_version="9.9.9")
    mutations.append((unknown_schema, "schema version"))
    unknown_definition = dict(valid, definition_version="9.9.9")
    mutations.append((unknown_definition, "definition version"))
    no_source = dict(valid)
    no_source.pop("source")
    mutations.append((no_source, "source provenance"))
    no_evidence = json.loads(json.dumps(valid))
    no_evidence["source"]["evidence_refs"] = []
    mutations.append((no_evidence, "watermarks and evidence"))
    missing_value = dict(valid, numerator=None)
    mutations.append((missing_value, "numeric numerator"))
    nonfinite = dict(valid, numerator=float("nan"))
    mutations.append((nonfinite, "finite nonnegative"))

    for observation, message in mutations:
        with pytest.raises(MetricContractError, match=message):
            calculate_metric(case["metric_id"], observation)
    with pytest.raises(MetricContractError, match="unknown metric id"):
        calculate_metric("unknown.metric", valid)


def test_denominators_confidence_policy_and_failure_evidence_are_non_bypassable() -> None:
    fixture = _fixture()
    cases = {case["metric_id"]: case for case in fixture["cases"]}

    ratio = _observation(cases["flow.review_coverage"], fixture)
    undefined = calculate_metric(
        "flow.review_coverage", dict(ratio, numerator=0, denominator=0)
    )
    assert undefined["truth_state"] == "unknown"
    assert undefined["value"] is None
    assert undefined["numerator"] == 0
    assert undefined["denominator"] == 0
    assert undefined["data_quality"]["errors"] == [
        "calculation denominator is zero or missing"
    ]

    estimate = _observation(cases["economy.cost_per_accepted_outcome"], fixture)
    with pytest.raises(MetricContractError, match="require confidence"):
        calculate_metric("economy.cost_per_accepted_outcome", dict(estimate, confidence=None))

    protected = _observation(cases["legal.global_program_status"], fixture)
    with pytest.raises(MetricContractError, match="policy decision"):
        calculate_metric("legal.global_program_status", dict(protected, policy_decision_ref=None))

    failed = next(case for case in fixture["cases"] if case["truth_state"] == "unavailable")
    unavailable = _observation(failed, fixture)
    unavailable["data_quality"] = dict(unavailable["data_quality"], errors=[])
    with pytest.raises(MetricContractError, match="safe error provenance"):
        calculate_metric(failed["metric_id"], unavailable)


def test_scope_visibility_confidence_and_ttl_cannot_emit_schema_invalid_results() -> None:
    fixture = _fixture()
    cases = {case["metric_id"]: case for case in fixture["cases"]}

    lane = _observation(cases["ai.accepted_outcome_rate"], fixture)
    lane["scope"] = {"portfolio_id": "sk-estate"}
    with pytest.raises(MetricContractError, match="exact definition dimensions"):
        calculate_metric("ai.accepted_outcome_rate", lane)

    visible_denied = _observation(fixture["cases"][0], fixture)
    visible_denied["visibility"] = {
        "state": "visible",
        "authorization": "denied",
        "policy_decision_ref": "policy:deny",
    }
    with pytest.raises(MetricContractError, match="must agree"):
        calculate_metric(fixture["cases"][0]["metric_id"], visible_denied)

    estimate = _observation(cases["economy.cost_per_accepted_outcome"], fixture)
    estimate["confidence"] = dict(estimate["confidence"], level=2)
    with pytest.raises(MetricContractError, match="between zero and one"):
        calculate_metric("economy.cost_per_accepted_outcome", estimate)

    bad_ttl = _observation(fixture["cases"][0], fixture)
    bad_ttl["source"] = dict(bad_ttl["source"], freshness_ttl_seconds=0)
    with pytest.raises(MetricContractError, match="positive integer"):
        calculate_metric(fixture["cases"][0]["metric_id"], bad_ttl)


def test_all_schema_shaped_boundaries_reject_wrong_types_lengths_and_ranges() -> None:
    fixture = _fixture()
    cases = {case["metric_id"]: case for case in fixture["cases"]}
    metric_id = fixture["cases"][0]["metric_id"]

    adapter_type = _observation(fixture["cases"][0], fixture)
    adapter_type["source"] = dict(adapter_type["source"], adapter_version=1)

    too_many_watermarks = _observation(fixture["cases"][0], fixture)
    too_many_watermarks["source"] = dict(
        too_many_watermarks["source"],
        watermarks=[{"source": "s", "value": str(index)} for index in range(65)],
    )

    long_evidence = _observation(fixture["cases"][0], fixture)
    long_evidence["source"] = dict(long_evidence["source"], evidence_refs=["x" * 513])

    numeric_scope = _observation(fixture["cases"][0], fixture)
    numeric_scope["scope"] = {"portfolio_id": 7}
    long_scope = _observation(fixture["cases"][0], fixture)
    long_scope["scope"] = {"portfolio_id": "x" * 129}

    numeric_baseline = _observation(fixture["cases"][0], fixture)
    numeric_baseline["window"] = dict(numeric_baseline["window"], baseline=7)

    numeric_reason = _observation(cases["legal.global_program_status"], fixture)
    numeric_reason["visibility"] = dict(numeric_reason["visibility"], reason=7)

    empty_error = _observation(cases["itil.change_classification_coverage"], fixture)
    empty_error["data_quality"] = dict(empty_error["data_quality"], errors=[""])

    numeric_note = _observation(fixture["cases"][0], fixture)
    numeric_note["data_quality"] = dict(numeric_note["data_quality"], notes=[7])

    policy_type = _observation(fixture["cases"][0], fixture)
    policy_type["policy_decision_ref"] = ["x"]

    invalid = (
        (metric_id, adapter_type, "adapter_version"),
        (metric_id, too_many_watermarks, "watermarks"),
        (metric_id, long_evidence, "evidence references"),
        (metric_id, numeric_scope, "scope portfolio_id"),
        (metric_id, long_scope, "scope portfolio_id"),
        (metric_id, numeric_baseline, "window baseline"),
        ("legal.global_program_status", numeric_reason, "visibility reason"),
        ("itil.change_classification_coverage", empty_error, "errors"),
        (metric_id, numeric_note, "notes"),
        (metric_id, policy_type, "classification policy_decision_ref"),
    )
    for selected_metric, observation, message in invalid:
        with pytest.raises(MetricContractError, match=message):
            calculate_metric(selected_metric, observation)

    forecast = _observation(cases["operator.ready_condition_forecast"], fixture)
    forecast["confidence"] = {"level": 0.7, "method": "condition table"}
    with pytest.raises(MetricContractError, match="ordered lower and upper"):
        calculate_metric("operator.ready_condition_forecast", forecast)


def test_final_result_edges_cannot_bypass_frozen_schema_or_canonical_json() -> None:
    fixture = _fixture()
    cases = {case["metric_id"]: case for case in fixture["cases"]}
    portfolio = _observation(fixture["cases"][0], fixture)

    no_coverage = json.loads(json.dumps(portfolio))
    no_coverage["data_quality"].pop("coverage_numerator")
    no_coverage["data_quality"].pop("coverage_denominator")
    with pytest.raises(MetricContractError, match="coverage fields"):
        calculate_metric(fixture["cases"][0]["metric_id"], no_coverage)

    malformed_confidence = dict(portfolio, confidence="garbage")
    with pytest.raises(MetricContractError, match="object or null"):
        calculate_metric(fixture["cases"][0]["metric_id"], malformed_confidence)

    not_applicable = next(
        case for case in fixture["cases"] if case["truth_state"] == "not_applicable"
    )
    hidden_na = _observation(not_applicable, fixture)
    hidden_na["visibility"] = {
        "state": "unauthorized",
        "authorization": "denied",
        "policy_decision_ref": "policy:deny",
        "reason": "scope denied",
    }
    with pytest.raises(MetricContractError, match="visibility decision"):
        calculate_metric(not_applicable["metric_id"], hidden_na)

    ratio = _observation(cases["flow.review_coverage"], fixture)
    error_overflow = dict(ratio, numerator=0, denominator=0)
    error_overflow["data_quality"] = dict(
        error_overflow["data_quality"], errors=[f"error {index}" for index in range(64)]
    )
    with pytest.raises(MetricContractError, match="error limit"):
        calculate_metric("flow.review_coverage", error_overflow)

    no_zero_evidence = dict(ratio, numerator=0, denominator=0)
    no_zero_evidence["source"] = dict(
        no_zero_evidence["source"], watermarks=[], evidence_refs=[]
    )
    with pytest.raises(MetricContractError, match="watermarks and evidence"):
        calculate_metric("flow.review_coverage", no_zero_evidence)

    overflow = _observation(cases["economy.cost_per_accepted_outcome"], fixture)
    overflow = dict(overflow, numerator=1e308, denominator=1e-308)
    with pytest.raises(MetricContractError, match="result must be finite"):
        calculate_metric("economy.cost_per_accepted_outcome", overflow)


def test_timestamp_types_and_temporal_order_are_deterministic() -> None:
    fixture = _fixture()
    case = fixture["cases"][0]
    metric_id = case["metric_id"]

    source_object = _observation(case, fixture)
    source_object["source"] = dict(
        source_object["source"], observed_at=datetime(2026, 8, 24, tzinfo=timezone.utc)
    )
    with pytest.raises(MetricContractError, match="ISO 8601 string"):
        calculate_metric(metric_id, source_object)

    window_object = _observation(case, fixture)
    window_object["window"] = dict(
        window_object["window"], start=datetime(2026, 8, 24, tzinfo=timezone.utc)
    )
    with pytest.raises(MetricContractError, match="ISO 8601 string"):
        calculate_metric(metric_id, window_object)

    reversed_window = _observation(case, fixture)
    reversed_window["window"] = dict(
        reversed_window["window"],
        start="2026-08-24T12:00:00Z",
        end="2026-08-23T12:00:00Z",
    )
    with pytest.raises(MetricContractError, match="end cannot precede start"):
        calculate_metric(metric_id, reversed_window)

    reversed_source = _observation(case, fixture)
    reversed_source["source"] = dict(
        reversed_source["source"],
        observed_at="2026-08-24T12:00:00Z",
        projected_at="2026-08-24T11:59:00Z",
    )
    with pytest.raises(MetricContractError, match="projected_at cannot precede observed_at"):
        calculate_metric(metric_id, reversed_source)


def test_measurement_lanes_stay_separate_and_no_individual_ranking_exists() -> None:
    fixture = _fixture()
    lane_cases = [case for case in fixture["cases"] if case.get("measurement_lane")]
    assert {case["measurement_lane"] for case in lane_cases} == {
        "harness_reported",
        "gateway_observed",
    }
    results = [
        calculate_metric(case["metric_id"], _observation(case, fixture)) for case in lane_cases
    ]
    assert {result["scope"]["measurement_lane"] for result in results} == {
        "harness_reported",
        "gateway_observed",
    }
    registry_text = json.dumps([definition.__dict__ for definition in DEFINITIONS]).lower()
    assert not any(term in registry_text for term in ("person_id", "user_id", "productivity", "rank"))

    observation = _observation(fixture["cases"][0], fixture)
    observation["scope"] = {"person_id": "someone"}
    with pytest.raises(MetricContractError, match="scope"):
        calculate_metric(fixture["cases"][0]["metric_id"], observation)
