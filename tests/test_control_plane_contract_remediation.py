from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver, ValidationError

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"
VERSIONED_CONTRACTS = CONTRACTS / "v1.1.0"
VERSION = "1.1.0"
EXPECTED_CONTRACTS = {
    "control-plane-action-preview.v1.1.0.schema.json",
    "control-plane-insight.v1.1.0.schema.json",
    "control-plane-metric-result.v1.1.0.schema.json",
    "control-plane-recommendation.v1.1.0.schema.json",
    "control-plane-report-snapshot.v1.1.0.schema.json",
    "openapi.control-plane.v1.1.0.json",
}


def _load(name: str) -> dict:
    return json.loads((VERSIONED_CONTRACTS / name).read_text(encoding="utf-8"))


def _metric(
    value=7,
    truth_state="current",
    evidence_refs=None,
    errors=None,
    watermarks=None,
    measurement_kind="measured",
) -> dict:
    return {
        "metric_id": "flow.throughput",
        "schema_version": "1.1.0",
        "definition_version": "1.0.0",
        "value": value,
        "unit": "items",
        "polarity": "higher_is_better",
        "scope": {},
        "window": {
            "start": "2026-08-23T00:00:00Z",
            "end": "2026-08-23T01:00:00Z",
            "timezone": "UTC",
        },
        "truth_state": truth_state,
        "visibility": {
            "state": "visible",
            "authorization": "authorized",
        },
        "measurement_kind": measurement_kind,
        "source": {
            "owner": "skcoord",
            "observed_at": "2026-08-23T01:00:00Z",
            "projected_at": "2026-08-23T01:01:00Z",
            "freshness_ttl_seconds": 300,
            "watermarks": watermarks
            if watermarks is not None
            else [{"source": "skcoord", "value": "w-1"}],
            "evidence_refs": evidence_refs or [],
        },
        "data_quality": {
            "coverage_numerator": 1,
            "coverage_denominator": 1,
            "errors": errors or [],
            "exclusions": [],
        },
        "calculation": {
            "definition_hash": "sha256:" + "a" * 64,
            "method": "deterministic fixture",
        },
        "classification": {"level": "internal"},
    }


def _validate(schema: dict, instance: dict) -> None:
    store = {
        path.as_uri(): json.loads(path.read_text(encoding="utf-8"))
        for path in VERSIONED_CONTRACTS.glob("*.json")
    }
    resolver = RefResolver(
        base_uri=VERSIONED_CONTRACTS.as_uri() + "/",
        referrer=schema,
        store=store,
    )
    Draft202012Validator(schema, resolver=resolver).validate(instance)


def _openapi_validator(document: dict, schema_name: str) -> Draft202012Validator:
    store = {
        path.as_uri(): json.loads(path.read_text(encoding="utf-8"))
        for path in VERSIONED_CONTRACTS.glob("*.json")
    }
    resolver = RefResolver(
        base_uri=VERSIONED_CONTRACTS.as_uri() + "/",
        referrer=document,
        store=store,
    )
    return Draft202012Validator(document["components"]["schemas"][schema_name], resolver=resolver)


def _external_refs(document: object) -> list[str]:
    refs: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                refs.append(ref.split("#", 1)[0])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return refs


def _projection(truth_state="current", errors=None) -> dict:
    return {
        "schema_version": "1.1.0",
        "request_id": "req-remediation-1",
        "source_owner": "skcoord",
        "scope": {},
        "freshness": {
            "truth_state": truth_state,
            "visibility": {
                "state": "visible",
                "authorization": "authorized",
            },
            "observed_at": "2026-08-23T01:00:00Z",
            "projected_at": "2026-08-23T01:01:00Z",
            "ttl_seconds": 300,
            "age_seconds": 60,
        },
        "visibility": {
            "state": "visible",
            "authorization": "authorized",
        },
        "metrics": [],
        "errors": errors or [],
    }


def _recommendation(recommendation_type="plan", status="proposed") -> dict:
    return {
        "recommendation_id": "rec-remediation-1",
        "schema_version": "1.1.0",
        "status": status,
        "title": "Inspect the bounded flow evidence",
        "recommendation_type": recommendation_type,
        "urgency": "this_cycle",
        "rationale": "The fixture has bounded evidence.",
        "metric_refs": ["flow.throughput"],
        "evidence_refs": ["evidence://flow/1"],
        "best_practice_refs": [
            {
                "framework": "SRE",
                "practice": "Use evidence-backed service objectives",
                "version": "2026.1",
                "reference": "https://example.invalid/sre/2026.1",
            }
        ],
        "expected_impact": [
            {
                "metric_id": "flow.throughput",
                "direction": "increase",
                "range": "1-5 percent",
                "horizon": "one cycle",
                "method": "fixture estimate",
            }
        ],
        "confidence": {"level": 0.8, "basis": "direct evidence"},
        "risks": ["Capacity could be constrained."],
        "counter_indicators": ["Throughput may be seasonal."],
        "alternatives": [{"label": "Observe", "tradeoff": "Slower learning."}],
        "preconditions": ["Confirm the source watermark."],
        "next_step": {"kind": "open_evidence", "label": "Open evidence"},
    }


def _action_preview(
    status="ready",
    action_class="read_only",
    target=None,
    expected_version="v-1",
    required_approvals=None,
) -> dict:
    return {
        "preview_id": "apv-remediation-1",
        "preview_hash": "sha256:" + "b" * 64,
        "schema_version": "1.1.0",
        "status": status,
        "action_class": action_class,
        "source_recommendation_id": "rec-remediation-1",
        "action_contract_id": "fixture.inspect",
        "action_contract_version": "1.0.0",
        "owner_service": "skcoord",
        "owner_operation": "inspect",
        "target": target if target is not None else {"scope": "fixture"},
        "expected_version": expected_version,
        "before_summary": "No state change.",
        "proposed_effect": "Read one bounded fixture.",
        "blast_radius": "None",
        "risk": {"level": "low", "reasons": []},
        "reversibility": "automatic",
        "verification_plan": ["Verify the receipt."],
        "rollback_plan": ["No rollback is needed."],
        "required_scope": "control-plane.read",
        "required_approvals": required_approvals if required_approvals is not None else [],
        "policy_decision_ref": "policy://fixture/allow",
        "expires_at": "2026-08-23T02:00:00Z",
    }


def _insight(
    status="proposal",
    metric_refs=None,
    evidence_refs=None,
    calculation_refs=None,
    uncertainty=None,
    recommendations=None,
    next_steps=None,
) -> dict:
    return {
        "insight_id": "ins-remediation-1",
        "schema_version": "1.1.0",
        "status": status,
        "kind": "metric_explanation",
        "summary": "The bounded fixture remains within its evidence window.",
        "scope": {},
        "window": {
            "start": "2026-08-23T00:00:00Z",
            "end": "2026-08-23T01:00:00Z",
            "timezone": "UTC",
        },
        "metric_refs": metric_refs if metric_refs is not None else ["flow.throughput"],
        "evidence_refs": evidence_refs if evidence_refs is not None else ["evidence://flow/1"],
        "calculation_refs": (
            calculation_refs if calculation_refs is not None else ["calculation://flow/1"]
        ),
        "uncertainty": (
            uncertainty if uncertainty is not None else ["Fixture uncertainty is bounded."]
        ),
        "exclusions": [],
        "visibility": {
            "state": "visible",
            "authorization": "authorized",
        },
        "model_provenance": {
            "logical_route": "skdashboard.insight",
            "transport_profile": "fixture-local",
            "gateway_revision": "gw-r1",
            "backend": "fixture",
            "requested_model": "model-fixture",
            "served_model": "model-fixture",
            "model_revision": "model-r1",
            "prompt_hash": "sha256:" + "c" * 64,
            "schema_hash": "sha256:" + "d" * 64,
        },
        "policy_decision_ref": "policy://fixture/allow",
        "recommendations": recommendations if recommendations is not None else [],
        "next_steps": (
            next_steps
            if next_steps is not None
            else [
                {
                    "label": "Open evidence",
                    "kind": "open_evidence",
                    "preview_only": True,
                }
            ]
        ),
    }


def _report_snapshot(truth_state="current", watermark=None) -> dict:
    return {
        "snapshot_id": "rpt-remediation-1",
        "schema_version": "1.1.0",
        "report_type": "daily_operations",
        "audience": ["operators"],
        "generated_at": "2026-08-23T01:01:00Z",
        "as_of": "2026-08-23T01:00:00Z",
        "scope": {},
        "metric_definition_hashes": {"flow.throughput": "sha256:" + "a" * 64},
        "source_watermarks": [
            watermark if watermark is not None else {"source": "skcoord", "value": "w-1"}
        ],
        "quality_statement": {
            "truth_state": truth_state,
            "visibility": {"state": "visible", "authorization": "authorized"},
            "summary": "The fixture is bounded.",
            "errors": [],
            "exclusions": [],
        },
        "sections": [
            {
                "section_id": "flow",
                "title": "Flow",
                "metric_results": [],
                "insights": [],
            }
        ],
        "review_state": {"state": "unreviewed"},
        "report_hash": "sha256:" + "b" * 64,
    }


def test_metric_zero_requires_evidence_and_failed_states_have_no_value() -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    valid_zero = _metric(value=0, evidence_refs=["evidence://flow/zero"])
    _validate(schema, valid_zero)

    no_evidence_zero = deepcopy(valid_zero)
    no_evidence_zero["source"]["evidence_refs"] = []
    with pytest.raises(ValidationError):
        _validate(schema, no_evidence_zero)

    failed = _metric(value=None, truth_state="unavailable", errors=["adapter timeout"])
    _validate(schema, failed)


@pytest.mark.parametrize(
    "truth_state", ["unavailable", "unreachable", "unknown", "not_applicable"]
)
@pytest.mark.parametrize("value", [0, "unknown"])
def test_non_observed_truth_states_reject_numeric_and_text_values(truth_state, value) -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    with pytest.raises(ValidationError):
        _validate(schema, _metric(value=value, truth_state=truth_state))


def test_measured_truth_values_require_evidence_watermarks_and_clean_current_data() -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    _validate(
        schema,
        _metric(
            value=7,
            truth_state="current",
            evidence_refs=["evidence://flow/current"],
        ),
    )
    _validate(
        schema,
        _metric(
            value=7,
            truth_state="partial",
            evidence_refs=["evidence://flow/partial"],
            errors=["one source was unavailable"],
        ),
    )
    _validate(schema, _metric(value=None, truth_state="unreachable"))

    for truth_state in ("current", "stale", "partial"):
        missing_evidence = _metric(value=7, truth_state=truth_state)
        with pytest.raises(ValidationError):
            _validate(schema, missing_evidence)

        missing_watermark = _metric(
            value=7,
            truth_state=truth_state,
            evidence_refs=["evidence://flow/value"],
            watermarks=[],
        )
        with pytest.raises(ValidationError):
            _validate(schema, missing_watermark)

    current_error = _metric(
        value=7,
        truth_state="current",
        evidence_refs=["evidence://flow/current"],
        errors=["source timeout"],
    )
    with pytest.raises(ValidationError):
        _validate(schema, current_error)

    numeric_unreachable = _metric(value=7, truth_state="unreachable")
    with pytest.raises(ValidationError):
        _validate(schema, numeric_unreachable)


def test_report_quality_and_watermark_invariants_include_unreachable() -> None:
    schema = _load("control-plane-report-snapshot.v1.1.0.schema.json")
    _validate(schema, _report_snapshot())
    _validate(schema, _report_snapshot(truth_state="unreachable"))

    partial_with_error = _report_snapshot(truth_state="partial")
    partial_with_error["quality_statement"]["errors"] = ["source timeout"]
    _validate(schema, partial_with_error)

    current_with_error = _report_snapshot()
    current_with_error["quality_statement"]["errors"] = ["source timeout"]
    with pytest.raises(ValidationError):
        _validate(schema, current_with_error)

    for field in ("source", "value"):
        invalid = _report_snapshot(watermark={"source": "skcoord", "value": "w-1"})
        invalid["source_watermarks"][0][field] = ""
        with pytest.raises(ValidationError):
            _validate(schema, invalid)


def test_partial_value_and_missing_value_are_distinct() -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    _validate(
        schema,
        _metric(
            value=0,
            truth_state="partial",
            evidence_refs=["evidence://partial"],
            errors=["node missing"],
        ),
    )

    missing = _metric(value=None, truth_state="unknown")
    _validate(schema, missing)
    del missing["value"]
    with pytest.raises(ValidationError):
        _validate(schema, missing)


def test_visibility_and_truth_are_separate_dimensions() -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    filtered = _metric(value=None, truth_state="unknown")
    filtered["visibility"] = {
        "state": "policy_filtered",
        "authorization": "denied",
        "policy_decision_ref": "policy://fixture/deny",
        "reason": "Matter membership is not visible at this scope.",
    }
    _validate(schema, filtered)

    mapped_truth = deepcopy(filtered)
    mapped_truth["truth_state"] = "not_applicable"
    with pytest.raises(ValidationError):
        _validate(schema, mapped_truth)

    missing_visibility = deepcopy(filtered)
    del missing_visibility["visibility"]
    with pytest.raises(ValidationError):
        _validate(schema, missing_visibility)

    for state, authorization in (
        ("visible", "denied"),
        ("policy_filtered", "authorized"),
    ):
        inconsistent = deepcopy(filtered)
        inconsistent["visibility"]["state"] = state
        inconsistent["visibility"]["authorization"] = authorization
        with pytest.raises(ValidationError):
            _validate(schema, inconsistent)

    for policy_ref in (None, ""):
        invalid_ref = deepcopy(filtered)
        invalid_ref["visibility"]["policy_decision_ref"] = policy_ref
        with pytest.raises(ValidationError):
            _validate(schema, invalid_ref)


def test_projection_errors_cannot_claim_current() -> None:
    document = _load("openapi.control-plane.v1.1.0.json")
    projection = document["components"]["schemas"]["ProjectionEnvelope"]
    metric_name = "control-plane-metric-result.v1.1.0.schema.json"
    resolver = RefResolver(
        base_uri=VERSIONED_CONTRACTS.as_uri() + "/",
        referrer=document,
        store={(VERSIONED_CONTRACTS / metric_name).as_uri(): _load(metric_name)},
    )

    Draft202012Validator(projection, resolver=resolver).validate(_projection())
    valid_error = {
        "code": "SOURCE_TIMEOUT",
        "message": "timed out",
        "retryable": True,
        "request_id": "req-remediation-1",
        "evidence_ref": "evidence://source/timeout",
    }
    Draft202012Validator(projection, resolver=resolver).validate(
        _projection("partial", [valid_error])
    )
    with pytest.raises(ValidationError):
        Draft202012Validator(projection, resolver=resolver).validate(
            _projection(
                "current",
                [valid_error],
            )
        )

    error_schema = document["components"]["schemas"]["Error"]
    for field in ("message", "request_id", "evidence_ref"):
        invalid_error = deepcopy(valid_error)
        invalid_error[field] = ""
        with pytest.raises(ValidationError):
            Draft202012Validator(error_schema).validate(invalid_error)

    metric_schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    assert (
        metric_schema["properties"]["truth_state"]["enum"]
        == document["components"]["schemas"]["TruthState"]["enum"]
    )


def test_action_recommendations_require_grounding_or_abstain() -> None:
    schema = _load("control-plane-recommendation.v1.1.0.schema.json")
    _validate(schema, _recommendation())

    empty_grounding = _recommendation()
    for field in (
        "best_practice_refs",
        "expected_impact",
        "risks",
        "counter_indicators",
        "alternatives",
        "preconditions",
    ):
        empty_grounding[field] = []
    with pytest.raises(ValidationError):
        _validate(schema, empty_grounding)

    abstained = _recommendation(status="abstained")
    for field in (
        "best_practice_refs",
        "expected_impact",
        "risks",
        "counter_indicators",
        "alternatives",
        "preconditions",
    ):
        abstained[field] = []
    abstained["abstention_reason"] = "Insufficient source evidence."
    _validate(schema, abstained)


def test_proposed_next_step_intent_requires_action_grounding() -> None:
    schema = _load("control-plane-recommendation.v1.1.0.schema.json")
    grounding_fields = (
        "best_practice_refs",
        "expected_impact",
        "risks",
        "counter_indicators",
        "alternatives",
        "preconditions",
    )

    valid_inspect = _recommendation(recommendation_type="inspect")
    valid_inspect["next_step"]["kind"] = "open_evidence"
    for field in grounding_fields:
        valid_inspect[field] = []
    _validate(schema, valid_inspect)

    inspect_preview = deepcopy(valid_inspect)
    inspect_preview["next_step"].update(
        {
            "kind": "preview_action",
            "target_ref": "card://fixture/1",
            "action_contract_id": "card.move",
            "parameter_proposal_ref": "proposal://fixture/1",
        }
    )
    with pytest.raises(ValidationError):
        _validate(schema, inspect_preview)
    weakened_preview = deepcopy(schema)
    preview_kinds = weakened_preview["allOf"][0]["if"]["anyOf"][1]["properties"]["next_step"][
        "properties"
    ]["kind"]["enum"]
    preview_kinds.remove("preview_action")
    _validate(weakened_preview, inspect_preview)

    inspect_draft = deepcopy(valid_inspect)
    inspect_draft["next_step"]["kind"] = "draft_report"
    with pytest.raises(ValidationError):
        _validate(schema, inspect_draft)
    weakened_draft = deepcopy(schema)
    draft_kinds = weakened_draft["allOf"][0]["if"]["anyOf"][1]["properties"]["next_step"][
        "properties"
    ]["kind"]["enum"]
    draft_kinds.remove("draft_report")
    _validate(weakened_draft, inspect_draft)

    insight_schema = _load("control-plane-insight.v1.1.0.schema.json")
    with pytest.raises(ValidationError):
        _validate(
            insight_schema,
            _insight(recommendations=[inspect_preview], next_steps=[]),
        )
    with pytest.raises(ValidationError):
        _validate(
            insight_schema,
            _insight(recommendations=[inspect_draft], next_steps=[]),
        )


def test_preview_action_next_step_requires_nonempty_refs() -> None:
    schema = _load("control-plane-recommendation.v1.1.0.schema.json")
    fields = ("target_ref", "action_contract_id", "parameter_proposal_ref")
    valid = _recommendation(recommendation_type="inspect")
    valid["next_step"].update(
        {
            "kind": "preview_action",
            "target_ref": "card://fixture/1",
            "action_contract_id": "card.move",
            "parameter_proposal_ref": "proposal://fixture/1",
        }
    )
    _validate(schema, valid)

    for field in fields:
        omitted = deepcopy(valid)
        omitted["next_step"].pop(field)
        _must_reject(schema, omitted)
        weakened = deepcopy(schema)
        weakened["allOf"][3]["then"]["properties"]["next_step"]["required"].remove(field)
        _validate(weakened, omitted)

        null_value = deepcopy(valid)
        null_value["next_step"][field] = None
        _must_reject(schema, null_value)
        weakened = deepcopy(schema)
        del weakened["allOf"][3]["then"]["properties"]["next_step"]["properties"][field]["type"]
        _validate(weakened, null_value)

        blank = deepcopy(valid)
        blank["next_step"][field] = " "
        _must_reject(schema, blank)
        weakened = deepcopy(schema)
        del weakened["allOf"][3]["then"]["properties"]["next_step"]["properties"][field]["pattern"]
        _validate(weakened, blank)


def test_proposal_insights_require_grounding_policy_and_model_provenance() -> None:
    schema = _load("control-plane-insight.v1.1.0.schema.json")
    _validate(schema, _insight())

    for field, empty in (
        ("summary", ""),
        ("metric_refs", []),
        ("evidence_refs", []),
        ("calculation_refs", []),
        ("uncertainty", []),
        ("policy_decision_ref", ""),
    ):
        invalid = _insight()
        invalid[field] = empty
        with pytest.raises(ValidationError):
            _validate(schema, invalid)

    invalid_provenance = _insight()
    invalid_provenance["model_provenance"]["served_model"] = ""
    with pytest.raises(ValidationError):
        _validate(schema, invalid_provenance)

    for state, authorization in (
        ("policy_filtered", "denied"),
        ("unauthorized", "unknown"),
        ("unknown", "unknown"),
    ):
        invalid_visibility = _insight()
        invalid_visibility["visibility"] = {
            "state": state,
            "authorization": authorization,
            "policy_decision_ref": "policy://fixture/deny",
            "reason": "The proposal is not visible at this scope.",
        }
        with pytest.raises(ValidationError):
            _validate(schema, invalid_visibility)

    no_outcome = _insight()
    no_outcome["recommendations"] = []
    no_outcome["next_steps"] = []
    with pytest.raises(ValidationError):
        _validate(schema, no_outcome)

    for field, value in (
        ("metric_refs", [""]),
        ("evidence_refs", [""]),
        ("calculation_refs", [""]),
        ("uncertainty", [""]),
    ):
        invalid_nested = _insight()
        invalid_nested[field] = value
        with pytest.raises(ValidationError):
            _validate(schema, invalid_nested)

    for field in ("framework", "practice", "version", "reference"):
        invalid_nested = _insight(
            recommendations=[_recommendation()],
            next_steps=[],
        )
        invalid_nested["recommendations"][0]["best_practice_refs"][0][field] = ""
        with pytest.raises(ValidationError):
            _validate(schema, invalid_nested)

    for field in ("metric_id", "range", "horizon", "method"):
        invalid_nested = _insight(
            recommendations=[_recommendation()],
            next_steps=[],
        )
        invalid_nested["recommendations"][0]["expected_impact"][0][field] = ""
        with pytest.raises(ValidationError):
            _validate(schema, invalid_nested)

    invalid_nested = _insight(recommendations=[_recommendation()], next_steps=[])
    invalid_nested["recommendations"][0]["evidence_refs"] = [""]
    with pytest.raises(ValidationError):
        _validate(schema, invalid_nested)


def test_abstained_insights_have_typed_reason_and_no_ready_next_step() -> None:
    schema = _load("control-plane-insight.v1.1.0.schema.json")
    abstained = _insight(status="abstained")
    abstained["evidence_refs"] = []
    abstained["calculation_refs"] = []
    abstained["uncertainty"] = []
    abstained["abstention_reason"] = {
        "code": "insufficient_evidence",
        "message": "The source window has insufficient evidence.",
    }
    _validate(schema, abstained)

    filtered_abstention = deepcopy(abstained)
    filtered_abstention["visibility"] = {
        "state": "policy_filtered",
        "authorization": "denied",
        "policy_decision_ref": "policy://fixture/deny",
        "reason": "The source is policy filtered.",
    }
    _validate(schema, filtered_abstention)

    missing_reason = deepcopy(abstained)
    del missing_reason["abstention_reason"]
    with pytest.raises(ValidationError):
        _validate(schema, missing_reason)

    untyped_reason = deepcopy(abstained)
    untyped_reason["abstention_reason"] = "Insufficient evidence."
    with pytest.raises(ValidationError):
        _validate(schema, untyped_reason)

    ready_step = deepcopy(abstained)
    ready_step["next_steps"] = [
        {"label": "Run command", "kind": "preview_command", "preview_only": True}
    ]
    with pytest.raises(ValidationError):
        _validate(schema, ready_step)


def test_composed_insight_outcomes_fail_closed() -> None:
    recommendation_schema = _load("control-plane-recommendation.v1.1.0.schema.json")
    abstained_recommendation = _recommendation(status="abstained")
    abstained_recommendation["abstention_reason"] = "Insufficient source evidence."
    abstained_recommendation["next_step"]["kind"] = "preview_action"
    with pytest.raises(ValidationError):
        _validate(recommendation_schema, abstained_recommendation)

    weakened_recommendation = deepcopy(recommendation_schema)
    del weakened_recommendation["allOf"][1]["then"]["properties"]["next_step"]
    _validate(weakened_recommendation, abstained_recommendation)

    safe_abstained_recommendation = deepcopy(abstained_recommendation)
    safe_abstained_recommendation["next_step"]["kind"] = "open_evidence"
    insight_schema = _load("control-plane-insight.v1.1.0.schema.json")
    abstained_insight = _insight(
        status="abstained",
        recommendations=[safe_abstained_recommendation],
    )
    abstained_insight["evidence_refs"] = []
    abstained_insight["calculation_refs"] = []
    abstained_insight["uncertainty"] = []
    abstained_insight["abstention_reason"] = {
        "code": "insufficient_evidence",
        "message": "The nested recommendation is not actionable.",
    }
    with pytest.raises(ValidationError):
        _validate(insight_schema, abstained_insight)

    weakened_insight = deepcopy(insight_schema)
    del weakened_insight["allOf"][1]["then"]["properties"]["recommendations"]["maxItems"]
    _validate(weakened_insight, abstained_insight)

    proposed_preview_recommendation = _recommendation()
    proposed_preview_recommendation["next_step"].update(
        {
            "kind": "preview_action",
            "target_ref": "card://fixture/1",
            "action_contract_id": "card.move",
            "parameter_proposal_ref": "proposal://fixture/1",
        }
    )
    abstained_with_proposed_preview = _insight(
        status="abstained",
        recommendations=[proposed_preview_recommendation],
    )
    abstained_with_proposed_preview["evidence_refs"] = []
    abstained_with_proposed_preview["calculation_refs"] = []
    abstained_with_proposed_preview["uncertainty"] = []
    abstained_with_proposed_preview["abstention_reason"] = {
        "code": "insufficient_evidence",
        "message": "Insufficient evidence.",
    }
    with pytest.raises(ValidationError):
        _validate(insight_schema, abstained_with_proposed_preview)
    _validate(weakened_insight, abstained_with_proposed_preview)

    proposal_with_abstained_recommendation = _insight(
        recommendations=[safe_abstained_recommendation],
        next_steps=[
            {
                "label": "Open evidence",
                "kind": "open_evidence",
                "preview_only": True,
            }
        ],
    )
    with pytest.raises(ValidationError):
        _validate(insight_schema, proposal_with_abstained_recommendation)

    abstained_preview_recommendation = _recommendation(status="abstained")
    abstained_preview_recommendation["abstention_reason"] = "Insufficient evidence."
    abstained_preview_recommendation["next_step"]["kind"] = "preview_action"
    proposal_with_abstained_preview = _insight(
        recommendations=[abstained_preview_recommendation],
        next_steps=[],
    )
    with pytest.raises(ValidationError):
        _validate(insight_schema, proposal_with_abstained_preview)

    weakened_insight = deepcopy(insight_schema)
    del weakened_insight["allOf"][0]["then"]["anyOf"][0]["properties"]["recommendations"][
        "contains"
    ]
    _validate(weakened_insight, proposal_with_abstained_recommendation)

    valid_proposal = _insight(
        recommendations=[_recommendation()],
        next_steps=[],
    )
    _validate(insight_schema, valid_proposal)


def test_sensitive_ready_preview_requires_exact_version_approval() -> None:
    schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    _validate(schema, _action_preview())

    bypass = _action_preview(action_class="high_risk")
    with pytest.raises(ValidationError):
        _validate(schema, bypass)

    approval_required = _action_preview(status="needs_approval", action_class="external")
    approval_required["required_approvals"] = [
        {
            "approval_type": "owner",
            "state": "required",
            "exact_version_required": True,
            "current": True,
        }
    ]
    _validate(schema, approval_required)

    approved = _action_preview(action_class="protected_matter")
    approved["required_approvals"] = [
        {
            "approval_type": "matter-owner",
            "state": "approved",
            "exact_version_required": True,
            "current": True,
        }
    ]
    _validate(schema, approved)

    denied = _action_preview(status="denied", action_class="external")
    denied["denial_reasons"] = ["Policy denied the destination."]
    _validate(schema, denied)

    expired = _action_preview(status="expired", action_class="external")
    _validate(schema, expired)


def test_ready_preview_requires_unanimous_current_approvals_and_policy_ref() -> None:
    schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    valid = _action_preview(action_class="external")
    valid["required_approvals"] = [
        {
            "approval_type": "owner",
            "state": "approved",
            "exact_version_required": True,
            "current": True,
        },
        {
            "approval_type": "security",
            "state": "approved",
            "exact_version_required": True,
            "current": True,
        },
    ]
    _validate(schema, valid)

    for field, value in (("state", "rejected"), ("state", "expired"), ("current", False)):
        invalid = deepcopy(valid)
        invalid["required_approvals"][1][field] = value
        with pytest.raises(ValidationError):
            _validate(schema, invalid)

    invalid_policy = deepcopy(valid)
    invalid_policy["policy_decision_ref"] = ""
    with pytest.raises(ValidationError):
        _validate(schema, invalid_policy)


def test_ready_mutating_preview_requires_target_identity_and_version() -> None:
    schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    approval = {
        "approval_type": "owner",
        "state": "approved",
        "exact_version_required": True,
        "current": True,
    }
    _validate(
        schema,
        _action_preview(
            action_class="external",
            required_approvals=[approval],
        ),
    )

    for field, value in (
        ("target", {"": "fixture"}),
        ("target", {"scope": " "}),
        ("expected_version", " "),
    ):
        invalid_identity = _action_preview(
            action_class="external",
            required_approvals=[approval],
        )
        invalid_identity[field] = value
        with pytest.raises(ValidationError):
            _validate(schema, invalid_identity)

    for action_class in ("low_risk_internal", "high_risk", "external"):
        missing_target = _action_preview(
            action_class=action_class,
            target={},
            required_approvals=[approval] if action_class != "low_risk_internal" else [],
        )
        with pytest.raises(ValidationError):
            _validate(schema, missing_target)

        empty_target_value = _action_preview(
            action_class=action_class,
            target={"scope": ""},
            required_approvals=[approval] if action_class != "low_risk_internal" else [],
        )
        with pytest.raises(ValidationError):
            _validate(schema, empty_target_value)

        missing_version = _action_preview(
            action_class=action_class,
            expected_version=None,
            required_approvals=[approval] if action_class != "low_risk_internal" else [],
        )
        with pytest.raises(ValidationError):
            _validate(schema, missing_version)

    _validate(schema, _action_preview(target={}, expected_version=None))


def _must_reject(schema: dict, instance: dict) -> None:
    try:
        _validate(schema, instance)
    except ValidationError:
        return
    raise AssertionError("counterexample was accepted")


def test_counterexample_sensitivity_requires_each_v11_guard() -> None:
    metric_schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    unreachable_counterexample = _metric(value=1, truth_state="unreachable")
    _must_reject(metric_schema, unreachable_counterexample)
    weakened_metric = deepcopy(metric_schema)
    weakened_metric["allOf"][0]["if"]["properties"]["truth_state"]["enum"].remove("unreachable")
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, unreachable_counterexample)

    no_evidence_counterexample = _metric(value=7)
    _must_reject(metric_schema, no_evidence_counterexample)
    weakened_metric = deepcopy(metric_schema)
    del weakened_metric["allOf"][3]["then"]["properties"]["source"]["properties"]["evidence_refs"][
        "minItems"
    ]
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, no_evidence_counterexample)

    no_watermark_counterexample = _metric(
        value=7,
        evidence_refs=["evidence://counterexample"],
        watermarks=[],
    )
    _must_reject(metric_schema, no_watermark_counterexample)
    weakened_metric = deepcopy(metric_schema)
    del weakened_metric["allOf"][3]["then"]["properties"]["source"]["properties"]["watermarks"][
        "minItems"
    ]
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, no_watermark_counterexample)

    current_error_counterexample = _metric(
        value=7,
        evidence_refs=["evidence://counterexample"],
        errors=["source timeout"],
    )
    _must_reject(metric_schema, current_error_counterexample)
    weakened_metric = deepcopy(metric_schema)
    del weakened_metric["allOf"][4]["then"]["properties"]["data_quality"]["properties"]["errors"][
        "maxItems"
    ]
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, current_error_counterexample)

    empty_evidence_ref = _metric(value=0, evidence_refs=[""])
    _must_reject(metric_schema, empty_evidence_ref)
    weakened_metric = deepcopy(metric_schema)
    del weakened_metric["$defs"]["source"]["properties"]["evidence_refs"]["items"]["minLength"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, empty_evidence_ref)

    recommendation_schema = _load("control-plane-recommendation.v1.1.0.schema.json")
    recommendation_counterexample = _recommendation()
    for field in (
        "best_practice_refs",
        "expected_impact",
        "risks",
        "counter_indicators",
        "alternatives",
        "preconditions",
    ):
        recommendation_counterexample[field] = []
    _must_reject(recommendation_schema, recommendation_counterexample)
    weakened_recommendation = deepcopy(recommendation_schema)
    weakened_recommendation["allOf"] = weakened_recommendation["allOf"][1:]
    with pytest.raises(AssertionError):
        _must_reject(weakened_recommendation, recommendation_counterexample)

    recommendation_ref_counterexample = _recommendation()
    recommendation_ref_counterexample["metric_refs"] = [""]
    _must_reject(recommendation_schema, recommendation_ref_counterexample)
    weakened_recommendation = deepcopy(recommendation_schema)
    del weakened_recommendation["properties"]["metric_refs"]["items"]["minLength"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_recommendation, recommendation_ref_counterexample)

    nested_ref_counterexample = _recommendation()
    nested_ref_counterexample["best_practice_refs"][0]["reference"] = ""
    _must_reject(recommendation_schema, nested_ref_counterexample)
    weakened_recommendation = deepcopy(recommendation_schema)
    del weakened_recommendation["properties"]["best_practice_refs"]["items"]["properties"][
        "reference"
    ]["minLength"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_recommendation, nested_ref_counterexample)

    impact_counterexample = _recommendation()
    impact_counterexample["expected_impact"][0]["metric_id"] = ""
    _must_reject(recommendation_schema, impact_counterexample)
    weakened_recommendation = deepcopy(recommendation_schema)
    del weakened_recommendation["properties"]["expected_impact"]["items"]["properties"][
        "metric_id"
    ]["minLength"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_recommendation, impact_counterexample)

    risks_counterexample = _recommendation()
    risks_counterexample["risks"] = []
    _must_reject(recommendation_schema, risks_counterexample)
    weakened_recommendation = deepcopy(recommendation_schema)
    del weakened_recommendation["allOf"][0]["then"]["properties"]["risks"]["minItems"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_recommendation, risks_counterexample)

    insight_schema = _load("control-plane-insight.v1.1.0.schema.json")
    provenance_counterexample = _insight()
    provenance_counterexample["model_provenance"]["gateway_revision"] = ""
    _must_reject(insight_schema, provenance_counterexample)
    weakened_insight = deepcopy(insight_schema)
    del weakened_insight["$defs"]["model_provenance"]["properties"]["gateway_revision"][
        "minLength"
    ]
    with pytest.raises(AssertionError):
        _must_reject(weakened_insight, provenance_counterexample)

    action_schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    action_counterexample = _action_preview(
        action_class="low_risk_internal",
        target={},
        expected_version=None,
    )
    _must_reject(action_schema, action_counterexample)
    weakened_action = deepcopy(action_schema)
    del weakened_action["allOf"][2]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, action_counterexample)

    empty_target_value = _action_preview(
        action_class="low_risk_internal",
        target={"scope": " "},
    )
    _must_reject(action_schema, empty_target_value)
    weakened_action = deepcopy(action_schema)
    del weakened_action["properties"]["target"]["additionalProperties"]["pattern"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, empty_target_value)

    blank_target_key = _action_preview(
        action_class="low_risk_internal",
        target={"": "fixture"},
    )
    _must_reject(action_schema, blank_target_key)
    weakened_action = deepcopy(action_schema)
    del weakened_action["properties"]["target"]["propertyNames"]["pattern"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, blank_target_key)

    whitespace_target_value = _action_preview(
        action_class="low_risk_internal",
        target={"scope": " "},
    )
    _must_reject(action_schema, whitespace_target_value)
    weakened_action = deepcopy(action_schema)
    del weakened_action["properties"]["target"]["additionalProperties"]["pattern"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, whitespace_target_value)

    whitespace_expected_version = _action_preview(
        action_class="low_risk_internal",
        expected_version=" ",
    )
    _must_reject(action_schema, whitespace_expected_version)
    weakened_action = deepcopy(action_schema)
    del weakened_action["allOf"][2]["then"]["properties"]["expected_version"]["pattern"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, whitespace_expected_version)

    approval_counterexample = _action_preview(action_class="high_risk")
    approval_counterexample["required_approvals"] = [
        {
            "approval_type": "owner",
            "state": "approved",
            "exact_version_required": False,
            "current": True,
        }
    ]
    _must_reject(action_schema, approval_counterexample)
    weakened_action = deepcopy(action_schema)
    for guard in (
        weakened_action["allOf"][0]["then"]["properties"]["required_approvals"]["items"],
        weakened_action["allOf"][1]["then"]["properties"]["required_approvals"]["items"],
        weakened_action["allOf"][1]["then"]["properties"]["required_approvals"]["contains"],
    ):
        del guard["properties"]["exact_version_required"]["const"]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, approval_counterexample)


def test_f8_each_distinct_guard_has_a_non_vacuous_sensitivity_probe() -> None:
    report_schema = _load("control-plane-report-snapshot.v1.1.0.schema.json")
    unreachable_report = _report_snapshot(truth_state="unreachable")
    _validate(report_schema, unreachable_report)
    weakened_report = deepcopy(report_schema)
    weakened_report["properties"]["quality_statement"]["properties"]["truth_state"]["enum"].remove(
        "unreachable"
    )
    with pytest.raises(ValidationError):
        _validate(weakened_report, unreachable_report)

    current_report_error = _report_snapshot()
    current_report_error["quality_statement"]["errors"] = ["source timeout"]
    _must_reject(report_schema, current_report_error)
    weakened_report = deepcopy(report_schema)
    del weakened_report["properties"]["quality_statement"]["allOf"][0]["then"]["properties"][
        "errors"
    ]["maxItems"]
    _validate(weakened_report, current_report_error)

    for field in ("source", "value"):
        invalid_watermark = _report_snapshot(watermark={"source": "skcoord", "value": "w-1"})
        invalid_watermark["source_watermarks"][0][field] = ""
        _must_reject(report_schema, invalid_watermark)
        weakened_report = deepcopy(report_schema)
        del weakened_report["properties"]["source_watermarks"]["items"]["properties"][field][
            "minLength"
        ]
        _validate(weakened_report, invalid_watermark)

    insight_schema = _load("control-plane-insight.v1.1.0.schema.json")
    missing_metric_ref = _insight(metric_refs=[])
    _must_reject(insight_schema, missing_metric_ref)
    weakened_insight = deepcopy(insight_schema)
    del weakened_insight["allOf"][0]["then"]["properties"]["metric_refs"]["minItems"]
    _validate(weakened_insight, missing_metric_ref)

    empty_metric_ref = _insight(metric_refs=[""])
    _must_reject(insight_schema, empty_metric_ref)
    weakened_insight = deepcopy(insight_schema)
    del weakened_insight["properties"]["metric_refs"]["items"]["minLength"]
    _validate(weakened_insight, empty_metric_ref)

    openapi = _load("openapi.control-plane.v1.1.0.json")
    valid_error = {
        "code": "SOURCE_TIMEOUT",
        "message": "timed out",
        "retryable": True,
        "request_id": "req-remediation-1",
        "evidence_ref": "evidence://source/timeout",
    }
    projection_validator = _openapi_validator(openapi, "ProjectionEnvelope")
    projection_validator.validate(_projection())
    current_projection_error = _projection("current", [valid_error])
    with pytest.raises(ValidationError):
        projection_validator.validate(current_projection_error)
    weakened_openapi = deepcopy(openapi)
    del weakened_openapi["components"]["schemas"]["ProjectionEnvelope"]["allOf"][0]
    _openapi_validator(weakened_openapi, "ProjectionEnvelope").validate(current_projection_error)

    error_schema = openapi["components"]["schemas"]["Error"]
    valid_error_validator = _openapi_validator(openapi, "Error")
    valid_error_validator.validate(valid_error)
    for field in ("message", "request_id", "evidence_ref"):
        invalid_error = deepcopy(valid_error)
        invalid_error[field] = ""
        with pytest.raises(ValidationError):
            valid_error_validator.validate(invalid_error)
        weakened_error = deepcopy(openapi)
        del weakened_error["components"]["schemas"]["Error"]["properties"][field]["minLength"]
        _openapi_validator(weakened_error, "Error").validate(invalid_error)
    assert error_schema["properties"]["message"]["minLength"] == 1

    unreachable_projection = _projection("unreachable")
    projection_validator.validate(unreachable_projection)
    weakened_openapi = deepcopy(openapi)
    weakened_openapi["components"]["schemas"]["TruthState"]["enum"].remove("unreachable")
    with pytest.raises(ValidationError):
        _openapi_validator(weakened_openapi, "ProjectionEnvelope").validate(unreachable_projection)

    action_request = {
        "recommendation_id": "rec-remediation-1",
        "action_contract_id": "fixture.inspect",
        "parameter_proposal_ref": "proposal://fixture/1",
    }
    action_request_validator = _openapi_validator(openapi, "ActionPreviewRequest")
    action_request_validator.validate(action_request)
    for field in action_request:
        invalid_request = deepcopy(action_request)
        invalid_request[field] = ""
        with pytest.raises(ValidationError):
            action_request_validator.validate(invalid_request)
        weakened_openapi = deepcopy(openapi)
        del weakened_openapi["components"]["schemas"]["ActionPreviewRequest"]["properties"][field][
            "minLength"
        ]
        _openapi_validator(weakened_openapi, "ActionPreviewRequest").validate(invalid_request)


def test_v11_filenames_ids_and_self_declared_versions_align() -> None:
    paths = sorted(VERSIONED_CONTRACTS.glob("*.json"))
    assert {path.name for path in paths} == EXPECTED_CONTRACTS
    version_pattern = re.compile(r"\.v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")

    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        match = version_pattern.search(path.name)
        assert match is not None, path.name
        assert match.group("version") == VERSION
        if path.name.startswith("openapi."):
            assert document["info"]["version"] == VERSION
            continue
        assert document["$id"].rsplit("/", 1)[-1] == path.name
        assert document["title"].endswith(f"version {VERSION}")
        assert document["properties"]["schema_version"]["const"] == VERSION


def test_v11_local_references_are_versioned_and_resolvable() -> None:
    paths = sorted(VERSIONED_CONTRACTS.glob("*.json"))
    names = {path.name for path in paths}
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        for reference in _external_refs(document):
            target = Path(reference).name
            assert target in names, f"{path.name} references missing {reference}"
            assert f".v{VERSION}" in target, f"{path.name} references unversioned {reference}"


def test_every_v11_contract_uses_draft_2020_12() -> None:
    for path in VERSIONED_CONTRACTS.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(document)


def test_v2_ui_normalization_is_documented() -> None:
    note = (CONTRACTS / "CONTROL-PLANE-CONTRACT-COMPATIBILITY-v1.1.0.md").read_text(
        encoding="utf-8"
    )
    for phrase in (
        "`Policy filtered`",
        "`visibility.state: policy_filtered`",
        "`visibility.authorization: denied`",
        "`not_applicable` is reserved",
        "`unreachable` is a distinct source",
        "nonempty evidence reference and source watermark",
        "at least one metric reference",
        "`service_id`",
        "`stale-target`",
        "`status: stale`",
        "`denied-policy`",
        "`status: denied`",
        "require re-preview",
        "`status: proposal`",
        "`status: abstained`",
        "`abstention_reason`",
        "marked `current`",
        "`exact_version_required: true`",
        "non-null exact expected version",
        "non-whitespace",
    ):
        assert phrase in note


def test_superseding_openapi_references_versioned_contracts() -> None:
    document = _load("openapi.control-plane.v1.1.0.json")
    assert document["info"]["version"] == VERSION
    assert (
        document["components"]["schemas"]["ProjectionEnvelope"]["properties"]["schema_version"][
            "const"
        ]
        == "1.1.0"
    )
    serialized = json.dumps(document)
    for name in (
        "control-plane-metric-result.v1.1.0.schema.json",
        "control-plane-action-preview.v1.1.0.schema.json",
        "control-plane-insight.v1.1.0.schema.json",
        "control-plane-report-snapshot.v1.1.0.schema.json",
    ):
        assert name in serialized
    assert "control-plane-recommendation.v1.1.0.schema.json" in json.dumps(
        _load("control-plane-insight.v1.1.0.schema.json")
    )
