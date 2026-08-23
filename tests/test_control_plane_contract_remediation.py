from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver

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


def _metric(value=7, truth_state="current", evidence_refs=None, errors=None) -> dict:
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
        "measurement_kind": "measured",
        "source": {
            "owner": "skcoord",
            "observed_at": "2026-08-23T01:00:00Z",
            "projected_at": "2026-08-23T01:01:00Z",
            "freshness_ttl_seconds": 300,
            "watermarks": [{"source": "skcoord", "value": "w-1"}],
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


def _action_preview(status="ready", action_class="read_only") -> dict:
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
        "target": {"scope": "fixture"},
        "expected_version": "v-1",
        "before_summary": "No state change.",
        "proposed_effect": "Read one bounded fixture.",
        "blast_radius": "None",
        "risk": {"level": "low", "reasons": []},
        "reversibility": "automatic",
        "verification_plan": ["Verify the receipt."],
        "rollback_plan": ["No rollback is needed."],
        "required_scope": "control-plane.read",
        "required_approvals": [],
        "policy_decision_ref": "policy://fixture/allow",
        "expires_at": "2026-08-23T02:00:00Z",
    }


def _insight(status="proposal") -> dict:
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
        "metric_refs": ["flow.throughput"],
        "evidence_refs": ["evidence://flow/1"],
        "calculation_refs": ["calculation://flow/1"],
        "uncertainty": ["Fixture uncertainty is bounded."],
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
        "recommendations": [],
        "next_steps": [
            {
                "label": "Open evidence",
                "kind": "open_evidence",
                "preview_only": True,
            }
        ],
    }


def test_metric_zero_requires_evidence_and_failed_states_have_no_value() -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    valid_zero = _metric(value=0, evidence_refs=["evidence://flow/zero"])
    _validate(schema, valid_zero)

    no_evidence_zero = deepcopy(valid_zero)
    no_evidence_zero["source"]["evidence_refs"] = []
    with pytest.raises(Exception):
        _validate(schema, no_evidence_zero)

    failed = _metric(value=None, truth_state="unavailable", errors=["adapter timeout"])
    _validate(schema, failed)


@pytest.mark.parametrize("truth_state", ["unavailable", "unknown", "not_applicable"])
@pytest.mark.parametrize("value", [0, "unknown"])
def test_non_observed_truth_states_reject_numeric_and_text_values(truth_state, value) -> None:
    schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    with pytest.raises(Exception):
        _validate(schema, _metric(value=value, truth_state=truth_state))


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
    with pytest.raises(Exception):
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
    with pytest.raises(Exception):
        _validate(schema, mapped_truth)

    missing_visibility = deepcopy(filtered)
    del missing_visibility["visibility"]
    with pytest.raises(Exception):
        _validate(schema, missing_visibility)

    for state, authorization in (
        ("visible", "denied"),
        ("policy_filtered", "authorized"),
    ):
        inconsistent = deepcopy(filtered)
        inconsistent["visibility"]["state"] = state
        inconsistent["visibility"]["authorization"] = authorization
        with pytest.raises(Exception):
            _validate(schema, inconsistent)

    for policy_ref in (None, ""):
        invalid_ref = deepcopy(filtered)
        invalid_ref["visibility"]["policy_decision_ref"] = policy_ref
        with pytest.raises(Exception):
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
    Draft202012Validator(projection, resolver=resolver).validate(
        _projection(
            "partial",
            [
                {
                    "code": "SOURCE_TIMEOUT",
                    "message": "timed out",
                    "retryable": True,
                    "request_id": "req-remediation-1",
                }
            ],
        )
    )
    with pytest.raises(Exception):
        Draft202012Validator(projection, resolver=resolver).validate(
            _projection(
                "current",
                [
                    {
                        "code": "SOURCE_TIMEOUT",
                        "message": "timed out",
                        "retryable": True,
                        "request_id": "req-remediation-1",
                    }
                ],
            )
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
    with pytest.raises(Exception):
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


def test_proposal_insights_require_grounding_policy_and_model_provenance() -> None:
    schema = _load("control-plane-insight.v1.1.0.schema.json")
    _validate(schema, _insight())

    for field, empty in (
        ("summary", ""),
        ("evidence_refs", []),
        ("calculation_refs", []),
        ("uncertainty", []),
        ("policy_decision_ref", ""),
    ):
        invalid = _insight()
        invalid[field] = empty
        with pytest.raises(Exception):
            _validate(schema, invalid)

    invalid_provenance = _insight()
    invalid_provenance["model_provenance"]["served_model"] = ""
    with pytest.raises(Exception):
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
        with pytest.raises(Exception):
            _validate(schema, invalid_visibility)

    no_outcome = _insight()
    no_outcome["recommendations"] = []
    no_outcome["next_steps"] = []
    with pytest.raises(Exception):
        _validate(schema, no_outcome)


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
    with pytest.raises(Exception):
        _validate(schema, missing_reason)

    untyped_reason = deepcopy(abstained)
    untyped_reason["abstention_reason"] = "Insufficient evidence."
    with pytest.raises(Exception):
        _validate(schema, untyped_reason)

    ready_step = deepcopy(abstained)
    ready_step["next_steps"] = [
        {"label": "Run command", "kind": "preview_command", "preview_only": True}
    ]
    with pytest.raises(Exception):
        _validate(schema, ready_step)


def test_sensitive_ready_preview_requires_exact_version_approval() -> None:
    schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    _validate(schema, _action_preview())

    bypass = _action_preview(action_class="high_risk")
    with pytest.raises(Exception):
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
        with pytest.raises(Exception):
            _validate(schema, invalid)

    invalid_policy = deepcopy(valid)
    invalid_policy["policy_decision_ref"] = ""
    with pytest.raises(Exception):
        _validate(schema, invalid_policy)


def _must_reject(schema: dict, instance: dict) -> None:
    try:
        _validate(schema, instance)
    except Exception:
        return
    raise AssertionError("counterexample was accepted")


def test_counterexample_sensitivity_requires_each_v11_guard() -> None:
    metric_schema = _load("control-plane-metric-result.v1.1.0.schema.json")
    metric_counterexample = _metric(
        value=0,
        truth_state="unavailable",
        evidence_refs=["evidence://counterexample"],
    )
    _must_reject(metric_schema, metric_counterexample)
    weakened_metric = deepcopy(metric_schema)
    weakened_metric["allOf"] = weakened_metric["allOf"][1:]
    with pytest.raises(AssertionError):
        _must_reject(weakened_metric, metric_counterexample)

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

    action_schema = _load("control-plane-action-preview.v1.1.0.schema.json")
    action_counterexample = _action_preview(action_class="external")
    _must_reject(action_schema, action_counterexample)
    weakened_action = deepcopy(action_schema)
    weakened_action["allOf"] = weakened_action["allOf"][2:]
    with pytest.raises(AssertionError):
        _must_reject(weakened_action, action_counterexample)


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
