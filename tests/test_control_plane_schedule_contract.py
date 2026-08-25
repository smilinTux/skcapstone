from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver, ValidationError

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts" / "schedule" / "v1.0.0"
EXPECTED = {
    "control-plane-reschedule-preview.v1.0.0.schema.json",
    "control-plane-schedule-insight.v1.0.0.schema.json",
    "control-plane-schedule-projection.v1.0.0.schema.json",
    "control-plane-schedule-scenario.v1.0.0.schema.json",
    "openapi.control-plane-schedule.v1.0.0.json",
}
HASH = "sha256:" + "a" * 64
VISIBLE = {"state": "visible", "authorization": "authorized"}
KNOWN = {"state": "known", "instant": "2026-08-24T12:00:00Z"}
UNKNOWN = {"state": "unknown", "instant": None, "reason": "owner date unavailable"}
NOT_APPLICABLE = {
    "state": "not_applicable",
    "instant": None,
    "reason": "not an actual date",
}


def _load(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def _validator(name: str) -> Draft202012Validator:
    document = _load(name)
    store = {
        path.as_uri(): json.loads(path.read_text(encoding="utf-8"))
        for path in CONTRACTS.glob("*.json")
    }
    return Draft202012Validator(
        document,
        resolver=RefResolver(base_uri=CONTRACTS.as_uri() + "/", referrer=document, store=store),
    )


def _dates(planned_target=KNOWN) -> dict:
    return {
        "baseline_start": KNOWN,
        "baseline_target": KNOWN,
        "planned_start": KNOWN,
        "planned_target": planned_target,
        "actual_start": UNKNOWN,
        "actual_finish": NOT_APPLICABLE,
    }


def _item(item_id="item-1", rollup_state="complete") -> dict:
    return {
        "item_id": item_id,
        "title": "Schedule item",
        "item_type": "project",
        "owner_service_id": "skcoord",
        "service_id": "skdashboard",
        "status": "doing",
        "truth_state": "current",
        "visibility": VISIBLE,
        "dates": _dates(),
        "baseline_variance": {"state": "known", "seconds": 0},
        "progress": 0.5,
        "progress_basis": "visible eligible children",
        "rollup": {
            "state": rollup_state,
            "eligible_children": 2,
            "included_children": 2 if rollup_state == "complete" else 1,
            "start": KNOWN,
            "end": KNOWN,
            "progress": 0.5,
            "progress_basis": "visible eligible children",
            "exclusions": [] if rollup_state == "complete" else ["policy-filtered child"],
        },
        "source_watermarks": [{"source": "skcoord", "value": "watermark-1"}],
        "evidence_refs": ["evidence://schedule/item-1"],
    }


def _projection() -> dict:
    return {
        "schema_version": "1.0.0",
        "projection_id": "schedule-1",
        "projection_version": "projection-v1",
        "projection_hash": HASH,
        "scope": {"role": "project_manager", "service_id": "skdashboard"},
        "display_timezone": "America/Chicago",
        "observed_at": "2026-08-24T12:00:00Z",
        "projected_at": "2026-08-24T12:01:00Z",
        "truth_state": "current",
        "visibility": VISIBLE,
        "source_watermarks": [{"source": "skcoord", "value": "watermark-1"}],
        "items": [_item()],
        "dependencies": [
            {
                "dependency_id": "dependency-1",
                "source_item_id": "item-1",
                "target_item_id": "item-2",
                "edge_type": "finish_to_start",
                "direction": "known",
                "lag_seconds": 0,
                "truth_state": "current",
                "visibility": VISIBLE,
                "blocker_state": "blocking",
                "cycle_state": "acyclic",
                "evidence_refs": ["evidence://schedule/dependency-1"],
            }
        ],
        "overlays": [
            {
                "overlay_id": "overlay-1",
                "overlay_type": "itil_change_window",
                "owner_service_id": "skcapstone",
                "start": KNOWN,
                "end": KNOWN,
                "truth_state": "current",
                "visibility": VISIBLE,
                "conflict_state": "clear",
                "evidence_refs": ["evidence://itil/change-1"],
            }
        ],
        "cycle_analysis": {"state": "acyclic", "cycle_item_ids": [], "evidence_refs": []},
        "critical_path": {"state": "available", "item_ids": ["item-1"], "reasons": []},
        "individual_ranking_prohibited": True,
        "errors": [],
    }


def _scenario() -> dict:
    empty_inputs = {
        "capacity": [],
        "dependency_slips": [],
        "milestone_moves": [],
        "itil_windows": [],
        "architecture_sequence": [],
    }
    return {
        "schema_version": "1.0.0",
        "scenario_id": "scn-schedule-1",
        "scenario_version": "scenario-v1",
        "scenario_hash": HASH,
        "source_projection_id": "schedule-1",
        "source_projection_version": "projection-v1",
        "source_projection_hash": HASH,
        "immutable": True,
        "mode": "no_write",
        "input_hash": HASH,
        "inputs": empty_inputs,
        "diff": [],
        "reset_ref": "schedule://scenario/scn-schedule-1/reset",
        "writes_owner_records": False,
        "created_at": "2026-08-24T12:02:00Z",
    }


def _preview() -> dict:
    return {
        "schema_version": "1.0.0",
        "preview_id": "rsp-schedule-1",
        "preview_hash": HASH,
        "status": "ready",
        "source_projection_id": "schedule-1",
        "source_projection_version": "projection-v1",
        "base_projection_hash": HASH,
        "base_hash_exact": True,
        "scenario_id": "scn-schedule-1",
        "scenario_hash": HASH,
        "proposal": [
            {
                "item_id": "item-1",
                "field": "planned_target",
                "before": "2026-08-24T12:00:00Z",
                "after": "2026-08-25T12:00:00Z",
                "evidence_refs": ["evidence://schedule/proposal-1"],
            }
        ],
        "policy_decision_ref": "policy://schedule/allow",
        "expires_at": "2026-08-24T12:07:00Z",
        "non_executing": True,
        "writes_owner_records": False,
    }


def test_contract_set_is_versioned_valid_and_locally_resolvable() -> None:
    paths = sorted(CONTRACTS.glob("*.json"))
    assert {path.name for path in paths} == EXPECTED
    for path in paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if path.name.startswith("openapi."):
            assert document["info"]["version"] == "1.0.0"
        else:
            Draft202012Validator.check_schema(document)
            assert document["$id"].endswith(path.name)
            assert document["properties"]["schema_version"]["const"] == "1.0.0"

    openapi = _load("openapi.control-plane-schedule.v1.0.0.json")
    serialized = json.dumps(openapi)
    for name in EXPECTED - {"openapi.control-plane-schedule.v1.0.0.json"}:
        assert name in serialized


def test_projection_preserves_timezone_null_dates_rollups_and_overlays() -> None:
    validator = _validator("control-plane-schedule-projection.v1.0.0.schema.json")
    validator.validate(_projection())

    missing_reason = _projection()
    missing_reason["items"][0]["dates"]["planned_target"] = {
        "state": "unknown",
        "instant": None,
    }
    with pytest.raises(ValidationError):
        validator.validate(missing_reason)

    dishonest_partial = _projection()
    dishonest_partial["items"][0] = _item(rollup_state="partial")
    dishonest_partial["items"][0]["rollup"]["exclusions"] = []
    with pytest.raises(ValidationError):
        validator.validate(dishonest_partial)

    policy_filtered = _projection()
    policy_filtered["items"][0]["visibility"] = {
        "state": "policy_filtered",
        "authorization": "denied",
        "policy_decision_ref": "policy://schedule/deny",
        "reason": "service scope denied",
    }
    policy_filtered["items"][0]["truth_state"] = "stale"
    validator.validate(policy_filtered)
    assert policy_filtered["items"][0]["truth_state"] == "stale"


def test_dependency_cycles_fail_closed_before_critical_path() -> None:
    validator = _validator("control-plane-schedule-projection.v1.0.0.schema.json")
    cyclic = _projection()
    cyclic["cycle_analysis"] = {
        "state": "cycles_detected",
        "cycle_item_ids": ["item-1", "item-2"],
        "evidence_refs": ["evidence://schedule/cycle-1"],
    }
    cyclic["critical_path"] = {
        "state": "unavailable",
        "item_ids": [],
        "reasons": ["dependency_cycle"],
    }
    validator.validate(cyclic)

    unsafe = deepcopy(cyclic)
    unsafe["critical_path"] = {"state": "available", "item_ids": ["item-1"], "reasons": []}
    with pytest.raises(ValidationError):
        validator.validate(unsafe)


def test_scenarios_are_immutable_no_write_records() -> None:
    validator = _validator("control-plane-schedule-scenario.v1.0.0.schema.json")
    validator.validate(_scenario())
    for field, value in (("immutable", False), ("mode", "write"), ("writes_owner_records", True)):
        invalid = _scenario()
        invalid[field] = value
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_reschedule_preview_requires_exact_version_hash_and_no_execution() -> None:
    validator = _validator("control-plane-reschedule-preview.v1.0.0.schema.json")
    validator.validate(_preview())

    stale = _preview()
    stale.update(status="stale", base_hash_exact=False, proposal=[])
    validator.validate(stale)

    unsafe = _preview()
    unsafe["base_hash_exact"] = False
    with pytest.raises(ValidationError):
        validator.validate(unsafe)

    for field, value in (("non_executing", False), ("writes_owner_records", True)):
        invalid = _preview()
        invalid[field] = value
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_schedule_insight_is_evidence_grounded_and_never_an_action() -> None:
    validator = _validator("control-plane-schedule-insight.v1.0.0.schema.json")
    insight = {
        "schema_version": "1.0.0", "insight_id": "insight-1", "forecast_ref": "forecast://1", "target_id": "release-1",
        "state": "available", "truth_state": "current", "policy_reference": "policy://schedule/allow", "source_versions": ["schedule:v1"],
        "engine_provenance": "engine://forecast/v1", "model_provenance": "model://schedule-explainer/v1", "reproducibility_key": "sha256:" + "b" * 64,
        "risk": "dependency slip", "explanation": "A blocker lies on the frozen path.", "support_evidence": ["evidence://1"],
        "counter_evidence": ["evidence://2"], "uncertainty": ["sample variance"],
        "affected_outcomes": ["milestone://1"], "alternatives": ["hold capacity"],
        "expected_impact": "one period", "action": "none", "writes_owner_records": False,
    }
    validator.validate(insight)
    invalid = dict(insight, action="reschedule")
    with pytest.raises(ValidationError):
        validator.validate(invalid)

    ambiguous = dict(insight, risk=None, explanation=None, expected_impact=None, support_evidence=[], counter_evidence=[], uncertainty=[], affected_outcomes=[], alternatives=[], abstention_reason="insufficient evidence")
    with pytest.raises(ValidationError):
        validator.validate(ambiguous)

    abstained = dict(ambiguous, state="abstained", model_provenance=None)
    validator.validate(abstained)
    for field in ("abstention_reason", "truth_state", "policy_reference", "source_versions", "engine_provenance", "reproducibility_key"):
        invalid = dict(abstained)
        invalid.pop(field)
        with pytest.raises(ValidationError):
            validator.validate(invalid)


def test_contract_forbids_individual_productivity_ranking() -> None:
    projection = _load("control-plane-schedule-projection.v1.0.0.schema.json")
    assert projection["properties"]["individual_ranking_prohibited"] == {"const": True}
    properties = json.dumps(projection).lower()
    for forbidden in (
        '"person_id"',
        '"user_id"',
        '"assignee_id"',
        '"productivity_score"',
        '"activity_score"',
        '"tokens_by_person"',
        '"commits_by_person"',
        '"joules_by_person"',
    ):
        assert forbidden not in properties
