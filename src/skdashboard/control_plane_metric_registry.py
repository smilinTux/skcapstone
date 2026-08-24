"""Versioned deterministic metric definitions for the control plane."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Mapping

SCHEMA_VERSION = "1.1.0"
REGISTRY_VERSION = "1.0.0"
NO_VALUE_STATES = frozenset({"unavailable", "unreachable", "unknown", "not_applicable"})
TRUTH_STATES = frozenset(
    {"current", "stale", "partial", "unavailable", "unreachable", "unknown", "not_applicable"}
)
MEASUREMENT_KINDS = frozenset({"measured", "derived", "estimated", "forecast"})
POLARITIES = frozenset({"higher_is_better", "lower_is_better", "target_range", "context_only"})
CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted"})
METHODS = frozenset({"count", "ratio", "ratio_percent"})
APPROVED_FAMILIES = (
    "portfolio",
    "flow",
    "itil_sre",
    "delivery",
    "architecture",
    "fleet",
    "ai_models",
    "economy",
    "governance",
    "legal",
    "corpus_pipeline",
    "operator_shell",
)


class MetricContractError(ValueError):
    """Raised when a definition or observation cannot produce a truthful result."""


def _string(value: object, field: str, *, maximum: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value) or len(value) > maximum:
        raise MetricContractError(f"{field} must be a valid string")
    return value


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise MetricContractError(f"{field} must be an ISO 8601 string")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MetricContractError(f"{field} must be ISO 8601") from error
    if instant.tzinfo is None:
        raise MetricContractError(f"{field} requires an explicit timezone")
    return instant


@dataclass(frozen=True)
class MetricDefinition:
    metric_id: str
    definition_version: str
    family: str
    label: str
    unit: str
    polarity: str
    measurement_kind: str
    method: str
    expression: str
    source_owner: str
    adapter_id: str
    classification: str = "internal"
    scope_dimensions: tuple[str, ...] = ("portfolio_id",)
    grain: str = "estate"
    target: dict | None = None
    calculation_inputs: tuple[str, ...] = (
        "numerator",
        "denominator",
        "sample_size",
        "exclusions",
    )

    @property
    def definition_hash(self) -> str:
        encoded = json.dumps(
            asdict(self), allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


DEFINITIONS = (
    MetricDefinition("portfolio.blocked_objectives", "1.0.0", "portfolio", "Blocked objectives", "objectives", "lower_is_better", "measured", "count", "numerator", "SKCapstone", "skcapstone.portfolio"),
    MetricDefinition("flow.review_coverage", "1.0.0", "flow", "Review coverage", "percent", "higher_is_better", "derived", "ratio_percent", "100 * numerator / denominator", "skcoord", "skcoord.flow"),
    MetricDefinition("itil.change_classification_coverage", "1.0.0", "itil_sre", "Change classification coverage", "percent", "higher_is_better", "derived", "ratio_percent", "100 * numerator / denominator", "SKCapstone ITIL", "skcapstone.itil"),
    MetricDefinition("engineering.delivery_signals_current", "1.0.0", "delivery", "Current delivery signals", "signals", "higher_is_better", "measured", "count", "numerator", "SKCapstone", "skcapstone.service_release"),
    MetricDefinition("architecture.drift_signals", "1.0.0", "architecture", "Material drift signals", "signals", "lower_is_better", "derived", "count", "numerator", "CMDB", "cmdb.configuration"),
    MetricDefinition("fleet.reporting_nodes", "1.0.0", "fleet", "Reporting fleet nodes", "nodes", "higher_is_better", "measured", "count", "numerator", "SKCapstone Fleet", "skcapstone.fleet"),
    MetricDefinition("ai.accepted_outcome_rate", "1.0.0", "ai_models", "Accepted AI outcome rate", "percent", "higher_is_better", "derived", "ratio_percent", "100 * numerator / denominator", "SKCounter", "skcounter.harness", scope_dimensions=("portfolio_id", "measurement_lane")),
    MetricDefinition("ai.gateway_observation_count", "1.0.0", "ai_models", "Gateway observation count", "observations", "context_only", "measured", "count", "numerator", "SKGateway", "skgateway.observed", scope_dimensions=("portfolio_id", "measurement_lane")),
    MetricDefinition("economy.cost_per_accepted_outcome", "1.0.0", "economy", "Cost per accepted outcome", "usd", "lower_is_better", "estimated", "ratio", "numerator / denominator", "SKCounter", "skcounter.harness", scope_dimensions=("portfolio_id", "measurement_lane")),
    MetricDefinition("governance.definition_coverage", "1.0.0", "governance", "Metric definition coverage", "percent", "higher_is_better", "derived", "ratio_percent", "100 * numerator / denominator", "CapAuth", "capauth.policy"),
    MetricDefinition("legal.global_program_status", "1.0.0", "legal", "Policy-filtered legal program status", "status", "context_only", "measured", "count", "numerator", "SKLegal", "sklegal.global", "confidential"),
    MetricDefinition("corpus.approved_release_health", "1.0.0", "corpus_pipeline", "Approved corpus release health", "releases", "higher_is_better", "measured", "count", "numerator", "HammerTime", "hammertime.pipeline", "confidential"),
    MetricDefinition("operator.ready_condition_forecast", "1.0.0", "operator_shell", "Ready condition forecast", "conditions", "context_only", "forecast", "count", "numerator", "Atlas", "atlas.conditions", "confidential"),
)


def _registry(
    definitions: tuple[MetricDefinition, ...] = DEFINITIONS,
) -> dict[tuple[str, str], MetricDefinition]:
    registry: dict[tuple[str, str], MetricDefinition] = {}
    for definition in definitions:
        key = (definition.metric_id, definition.definition_version)
        if key in registry:
            raise MetricContractError(f"duplicate metric definition: {key}")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{2,127}", definition.metric_id):
            raise MetricContractError(f"invalid metric id: {definition.metric_id}")
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", definition.definition_version):
            raise MetricContractError(f"invalid definition version: {definition.definition_version}")
        if definition.family not in APPROVED_FAMILIES:
            raise MetricContractError(f"unapproved metric family: {definition.family}")
        if definition.measurement_kind not in MEASUREMENT_KINDS:
            raise MetricContractError(f"unsupported measurement kind: {definition.measurement_kind}")
        if definition.polarity not in POLARITIES:
            raise MetricContractError(f"unsupported polarity: {definition.polarity}")
        if definition.classification not in CLASSIFICATIONS:
            raise MetricContractError(f"unsupported classification: {definition.classification}")
        if definition.method not in METHODS:
            raise MetricContractError(f"unsupported calculation method: {definition.method}")
        registry[key] = definition
    if {definition.family for definition in registry.values()} != set(APPROVED_FAMILIES):
        raise MetricContractError("registry must cover every approved estate silo")
    return registry


REGISTRY = _registry()


def registry_manifest() -> dict:
    """Return the stable hash-addressed registry manifest."""
    definitions = {
        f"{metric_id}@{version}": definition.definition_hash
        for (metric_id, version), definition in sorted(REGISTRY.items())
    }
    payload = {
        "registry_version": REGISTRY_VERSION,
        "metric_result_schema_version": SCHEMA_VERSION,
        "definition_hashes": definitions,
    }
    encoded = json.dumps(
        payload, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return {**payload, "registry_hash": f"sha256:{hashlib.sha256(encoded).hexdigest()}"}


def _require_provenance(source: object, *, evidence_required: bool) -> dict:
    if not isinstance(source, dict):
        raise MetricContractError("source provenance is required")
    required = {
        "owner",
        "adapter_id",
        "adapter_version",
        "observed_at",
        "projected_at",
        "freshness_ttl_seconds",
        "watermarks",
        "evidence_refs",
    }
    if required - source.keys():
        raise MetricContractError("source provenance is incomplete")
    if set(source) - required:
        raise MetricContractError("source provenance contains unknown fields")
    _string(source["owner"], "source owner", maximum=128)
    _string(source["adapter_id"], "source adapter_id", maximum=128)
    _string(source["adapter_version"], "source adapter_version", maximum=64)
    ttl = source["freshness_ttl_seconds"]
    if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1:
        raise MetricContractError("freshness_ttl_seconds must be a positive integer")
    observed_at = _timestamp(source["observed_at"], "source observed_at")
    projected_at = _timestamp(source["projected_at"], "source projected_at")
    if projected_at < observed_at:
        raise MetricContractError("source projected_at cannot precede observed_at")
    if evidence_required and (not source["watermarks"] or not source["evidence_refs"]):
        raise MetricContractError("observed values require watermarks and evidence")
    if (
        not isinstance(source["watermarks"], list)
        or len(source["watermarks"]) > 64
        or not all(
        isinstance(item, dict)
        and set(item) == {"source", "value"}
        and isinstance(item["source"], str)
        and bool(item["source"])
        and len(item["source"]) <= 128
        and isinstance(item["value"], str)
        and bool(item["value"])
        and len(item["value"]) <= 256
        for item in source["watermarks"]
        )
    ):
        raise MetricContractError("source watermarks are malformed")
    if (
        not isinstance(source["evidence_refs"], list)
        or len(source["evidence_refs"]) > 128
        or not all(
            isinstance(item, str) and bool(item) and len(item) <= 512
            for item in source["evidence_refs"]
        )
    ):
        raise MetricContractError("source evidence references are malformed")
    return dict(source)


def _calculate_value(definition: MetricDefinition, numerator: object, denominator: object) -> int | float:
    if (
        not isinstance(numerator, (int, float))
        or isinstance(numerator, bool)
        or not math.isfinite(numerator)
    ):
        raise MetricContractError("a numeric numerator is required")
    if definition.method == "count":
        return numerator
    if (
        not isinstance(denominator, (int, float))
        or isinstance(denominator, bool)
        or not math.isfinite(denominator)
    ):
        raise MetricContractError("a numeric denominator is required")
    if denominator <= 0:
        raise MetricContractError("denominator must be greater than zero")
    value = numerator / denominator
    value = value * 100 if definition.method == "ratio_percent" else value
    if not math.isfinite(value):
        raise MetricContractError("calculation result must be finite")
    return round(value, 6)


def calculate_metric(metric_id: str, observation: Mapping[str, object]) -> dict:
    """Calculate one canonical result without model-owned or inferred inputs."""
    definition_version = observation.get("definition_version")
    definition = REGISTRY.get((metric_id, str(definition_version)))
    if not any(key[0] == metric_id for key in REGISTRY):
        raise MetricContractError(f"unknown metric id: {metric_id}")
    if definition is None:
        raise MetricContractError("unknown metric definition version")
    if observation.get("schema_version") != SCHEMA_VERSION:
        raise MetricContractError("unknown metric result schema version")

    truth_state = observation.get("truth_state")
    if truth_state not in TRUTH_STATES:
        raise MetricContractError("unknown truth state")
    visibility = observation.get("visibility")
    if not isinstance(visibility, dict):
        raise MetricContractError("visibility provenance is required")
    if set(visibility) - {"state", "authorization", "policy_decision_ref", "reason"}:
        raise MetricContractError("visibility contains unknown fields")
    visibility_state = visibility.get("state")
    authorization = visibility.get("authorization")
    if visibility_state not in {
        "visible",
        "policy_filtered",
        "unauthorized",
        "redacted",
        "unknown",
    } or authorization not in {"authorized", "denied", "unknown"}:
        raise MetricContractError("unknown visibility state or authorization")
    if (visibility_state == "visible") != (authorization == "authorized"):
        raise MetricContractError("visible and authorized visibility must agree")
    if visibility_state != "visible" and not visibility.get("reason"):
        raise MetricContractError("non-visible results require a reason")
    if authorization == "denied" and not visibility.get("policy_decision_ref"):
        raise MetricContractError("denied visibility requires a policy decision reference")
    if truth_state == "not_applicable" and visibility_state != "visible":
        raise MetricContractError("not_applicable cannot represent a visibility decision")
    if visibility.get("reason") is not None:
        _string(visibility["reason"], "visibility reason", maximum=500)
    if visibility.get("policy_decision_ref") is not None:
        _string(
            visibility["policy_decision_ref"],
            "visibility policy_decision_ref",
            maximum=256,
        )
    numerator = observation.get("numerator")
    denominator = observation.get("denominator")
    sample_size = observation.get("sample_size")
    for name, value in (("numerator", numerator), ("denominator", denominator)):
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value < 0
        ):
            raise MetricContractError(f"{name} must be a finite nonnegative number or null")
    if sample_size is not None and (
        not isinstance(sample_size, int) or isinstance(sample_size, bool) or sample_size < 0
    ):
        raise MetricContractError("sample_size must be a nonnegative integer or null")
    undefined_ratio = (
        definition.method in {"ratio", "ratio_percent"}
        and truth_state not in NO_VALUE_STATES
        and visibility_state == "visible"
        and (denominator is None or denominator == 0)
    )
    if undefined_ratio:
        truth_state = "unknown"
    no_value = truth_state in NO_VALUE_STATES or visibility_state != "visible"
    if (
        no_value
        and not undefined_ratio
        and any(value is not None for value in (numerator, denominator, sample_size))
    ):
        raise MetricContractError("no-value truth states cannot carry observed calculation inputs")
    source = _require_provenance(
        observation.get("source"), evidence_required=not no_value or undefined_ratio
    )
    if source["owner"] != definition.source_owner or source["adapter_id"] != definition.adapter_id:
        raise MetricContractError("source provenance does not match the metric definition")

    scope = observation.get("scope")
    if not isinstance(scope, dict) or not scope:
        raise MetricContractError("an explicit metric scope is required")
    if set(scope) != set(definition.scope_dimensions):
        raise MetricContractError("scope must contain the exact definition dimensions")
    if any(key in scope for key in ("person_id", "user_id", "agent_id")):
        raise MetricContractError("individual scopes are not permitted")
    for key, value in scope.items():
        _string(value, f"scope {key}", maximum=64 if key == "measurement_lane" else 128, allow_empty=True)
    window = observation.get("window")
    if not isinstance(window, dict):
        raise MetricContractError("measurement window is required")
    if set(window) - {"start", "end", "timezone", "baseline"}:
        raise MetricContractError("window contains unknown fields")
    if not isinstance(window.get("timezone"), str) or not window["timezone"]:
        raise MetricContractError("window timezone is required")
    _string(window["timezone"], "window timezone", maximum=64, allow_empty=True)
    if window.get("baseline") is not None:
        _string(window["baseline"], "window baseline", maximum=128, allow_empty=True)
    window_start = _timestamp(window.get("start"), "window start")
    window_end = _timestamp(window.get("end"), "window end")
    if window_end < window_start:
        raise MetricContractError("window end cannot precede start")

    data_quality = observation.get("data_quality")
    if not isinstance(data_quality, dict):
        raise MetricContractError("data quality provenance is required")
    if set(data_quality) - {
        "coverage_numerator",
        "coverage_denominator",
        "errors",
        "exclusions",
        "notes",
    }:
        raise MetricContractError("data quality contains unknown fields")
    if {"coverage_numerator", "coverage_denominator"} - data_quality.keys():
        raise MetricContractError("data quality coverage fields are required")
    errors = data_quality.get("errors")
    exclusions = data_quality.get("exclusions")
    if not isinstance(errors, list) or not isinstance(exclusions, list):
        raise MetricContractError("data quality errors and exclusions are required")
    notes = data_quality.get("notes", [])
    for field, values, allow_empty in (
        ("errors", errors, False),
        ("exclusions", exclusions, False),
        ("notes", notes, True),
    ):
        if not isinstance(values, list) or len(values) > 64 or not all(
            isinstance(value, str)
            and (allow_empty or bool(value))
            and len(value) <= 256
            for value in values
        ):
            raise MetricContractError(f"data quality {field} are malformed")
    errors = list(errors)
    if undefined_ratio:
        if len(errors) == 64:
            raise MetricContractError("calculation error exceeds the data quality error limit")
        errors.append("calculation denominator is zero or missing")
    data_quality = {**data_quality, "errors": errors}
    if truth_state == "current" and errors:
        raise MetricContractError("current results cannot carry source errors")
    if truth_state in {"unavailable", "unreachable", "unknown"} and not errors:
        raise MetricContractError("failed truth states require safe error provenance")
    if truth_state == "not_applicable" and not exclusions:
        raise MetricContractError("not_applicable requires an explicit scope rationale")
    coverage_numerator = data_quality.get("coverage_numerator")
    coverage_denominator = data_quality.get("coverage_denominator")
    if coverage_numerator is not None or coverage_denominator is not None:
        if (
            not isinstance(coverage_numerator, int)
            or isinstance(coverage_numerator, bool)
            or not isinstance(coverage_denominator, int)
            or isinstance(coverage_denominator, bool)
            or coverage_numerator < 0
            or coverage_denominator < coverage_numerator
        ):
            raise MetricContractError("coverage must satisfy 0 <= numerator <= denominator")
    if truth_state == "partial" and not errors and coverage_numerator == coverage_denominator:
        raise MetricContractError("partial results require incomplete-population evidence")
    if definition.method == "ratio_percent" and not no_value:
        if numerator > denominator:
            raise MetricContractError("percentage numerator cannot exceed denominator")
    confidence = observation.get("confidence")
    if confidence is not None and not isinstance(confidence, dict):
        raise MetricContractError("confidence must be an object or null")
    if definition.measurement_kind in {"estimated", "forecast"} and not isinstance(
        confidence, dict
    ):
        raise MetricContractError("estimated and forecast results require confidence")
    if isinstance(confidence, dict):
        if set(confidence) - {"level", "lower", "upper", "method"}:
            raise MetricContractError("confidence contains unknown fields")
        level = confidence.get("level")
        if (
            not isinstance(level, (int, float))
            or isinstance(level, bool)
            or not math.isfinite(level)
            or not 0 <= level <= 1
        ):
            raise MetricContractError("confidence level must be between zero and one")
        _string(confidence.get("method"), "confidence method", maximum=160)
        for bound in ("lower", "upper"):
            value = confidence.get(bound)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise MetricContractError("confidence bounds must be finite numbers or null")
        if definition.measurement_kind == "forecast":
            lower = confidence.get("lower")
            upper = confidence.get("upper")
            if lower is None or upper is None or lower > upper:
                raise MetricContractError("forecast confidence requires ordered lower and upper bounds")
    policy_decision_ref = observation.get("policy_decision_ref")
    if policy_decision_ref is not None:
        _string(policy_decision_ref, "classification policy_decision_ref", maximum=256)
    if definition.classification in {"confidential", "restricted"} and not policy_decision_ref:
        raise MetricContractError("protected metric results require a policy decision reference")

    value = None if no_value else _calculate_value(definition, numerator, denominator)
    return {
        "metric_id": definition.metric_id,
        "schema_version": SCHEMA_VERSION,
        "definition_version": definition.definition_version,
        "label": definition.label,
        "value": value,
        "unit": definition.unit,
        "polarity": definition.polarity,
        "numerator": numerator,
        "denominator": denominator,
        "sample_size": sample_size,
        "scope": dict(scope),
        "grain": definition.grain,
        "window": dict(window),
        "target": definition.target,
        "truth_state": truth_state,
        "visibility": dict(visibility),
        "measurement_kind": definition.measurement_kind,
        "confidence": confidence,
        "source": source,
        "data_quality": dict(data_quality),
        "calculation": {
            "definition_hash": definition.definition_hash,
            "method": definition.method,
            "expression": definition.expression,
            "calculation_ref": (
                f"registry:{REGISTRY_VERSION}:{definition.metric_id}@{definition.definition_version}"
            ),
        },
        "classification": {
            "level": definition.classification,
            "policy_decision_ref": policy_decision_ref,
            "purpose": "control_plane_reporting",
        },
    }
