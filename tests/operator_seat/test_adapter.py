"""Tests for the O3a adapter contract conformance validator (pure)."""

from __future__ import annotations

from skcapstone.operator_seat.adapter import validate_explain, validate_observe


def _conformant_action() -> dict:
    return {
        "name": "restart_service",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "https://runbooks.example/restart_service",
        "kedb_refs": ["KEDB-1"],
    }


def _conformant_explain() -> dict:
    return {
        "kinds": ["Deployment", "StatefulSet"],
        "conditions": ["Available", "Progressing"],
        "actions": [_conformant_action()],
    }


def test_conformant_explain_has_no_violations():
    assert validate_explain(_conformant_explain()) == []


def test_kinds_must_be_a_list_of_str():
    payload = _conformant_explain()
    payload["kinds"] = "Deployment"
    assert validate_explain(payload) == ["kinds must be a list of str"]


def test_kinds_with_non_string_entry_is_flagged():
    payload = _conformant_explain()
    payload["kinds"] = ["Deployment", 1]
    assert validate_explain(payload) == ["kinds must be a list of str"]


def test_conditions_must_be_a_list_of_str():
    payload = _conformant_explain()
    payload["conditions"] = [{"type": "Available"}]
    assert validate_explain(payload) == ["conditions must be a list of str"]


def test_actions_must_be_a_list():
    payload = _conformant_explain()
    payload["actions"] = "restart_service"
    assert validate_explain(payload) == ["actions must be a list of dicts"]


def test_action_missing_key_is_flagged():
    payload = _conformant_explain()
    del payload["actions"][0]["runbook"]
    assert validate_explain(payload) == ["actions[0] missing key 'runbook'"]


def test_action_name_must_be_str():
    payload = _conformant_explain()
    payload["actions"][0]["name"] = 7
    assert validate_explain(payload) == ["actions[0].name must be a str"]


def test_action_standard_must_be_bool():
    payload = _conformant_explain()
    payload["actions"][0]["standard"] = "yes"
    assert validate_explain(payload) == ["actions[0].standard must be a bool"]


def test_action_reversible_must_be_bool():
    payload = _conformant_explain()
    payload["actions"][0]["reversible"] = "no"
    assert validate_explain(payload) == ["actions[0].reversible must be a bool"]


def test_action_runbook_must_be_str():
    payload = _conformant_explain()
    payload["actions"][0]["runbook"] = 123
    assert validate_explain(payload) == ["actions[0].runbook must be a str"]


def test_action_kedb_refs_must_be_list():
    payload = _conformant_explain()
    payload["actions"][0]["kedb_refs"] = "KEDB-1"
    assert validate_explain(payload) == ["actions[0].kedb_refs must be a list"]


def test_action_bad_blast_radius_is_flagged():
    payload = _conformant_explain()
    payload["actions"][0]["blast_radius"] = "catastrophic"
    violations = validate_explain(payload)
    assert len(violations) == 1
    assert "blast_radius" in violations[0]


def test_action_that_is_not_a_dict_is_flagged():
    payload = _conformant_explain()
    payload["actions"] = ["restart_service"]
    assert validate_explain(payload) == ["actions[0] must be a dict"]


def test_multiple_action_violations_are_all_reported():
    payload = _conformant_explain()
    payload["actions"][0] = {"name": 1}
    violations = validate_explain(payload)
    assert "actions[0] missing key 'standard'" in violations
    assert "actions[0] missing key 'reversible'" in violations
    assert "actions[0] missing key 'blast_radius'" in violations
    assert "actions[0] missing key 'runbook'" in violations
    assert "actions[0] missing key 'kedb_refs'" in violations
    assert "actions[0].name must be a str" in violations


def _conformant_observe() -> dict:
    return {
        "conditions": [
            {"type": "Available", "status": "True"},
            {"type": "Progressing", "status": "Unknown"},
        ]
    }


def test_conformant_observe_has_no_violations():
    assert validate_observe(_conformant_observe()) == []


def test_observe_conditions_must_be_a_list():
    payload = {"conditions": "Available"}
    assert validate_observe(payload) == ["conditions must be a list of dicts"]


def test_observe_condition_must_be_a_dict():
    payload = _conformant_observe()
    payload["conditions"][0] = "Available"
    assert validate_observe(payload) == ["conditions[0] must be a dict"]


def test_observe_condition_missing_type_is_flagged():
    payload = _conformant_observe()
    del payload["conditions"][0]["type"]
    assert validate_observe(payload) == ["conditions[0] missing key 'type'"]


def test_observe_condition_missing_status_is_flagged():
    payload = _conformant_observe()
    del payload["conditions"][0]["status"]
    assert validate_observe(payload) == ["conditions[0] missing key 'status'"]


def test_observe_bad_status_value_is_flagged():
    payload = _conformant_observe()
    payload["conditions"][0]["status"] = "maybe"
    violations = validate_observe(payload)
    assert len(violations) == 1
    assert "status" in violations[0]
