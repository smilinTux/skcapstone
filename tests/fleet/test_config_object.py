"""Tests for the Config kind: spec normalization and presence/drift/age conditions."""

from __future__ import annotations

import pytest

from skcapstone.fleet import config_object
from skcapstone.fleet.explain import explain

NOW = "2026-07-28T12:00:00Z"


def test_normalize_config_spec_defaults() -> None:
    spec = config_object.normalize_config_spec({"name": "gateway-tls"})
    assert spec == {
        "name": "gateway-tls",
        "secrets": [],
        "files": {},
        "rotationDays": None,
        "deleted": False,
    }


def test_normalize_config_spec_echoes_provided_fields() -> None:
    spec = config_object.normalize_config_spec(
        {
            "name": "gateway-tls",
            "secrets": ["gateway-tls-cert", "gateway-tls-key"],
            "files": {"/etc/gateway/tls.crt": "a" * 64},
            "rotationDays": 90,
            "deleted": True,
        }
    )
    assert spec == {
        "name": "gateway-tls",
        "secrets": ["gateway-tls-cert", "gateway-tls-key"],
        "files": {"/etc/gateway/tls.crt": "a" * 64},
        "rotationDays": 90,
        "deleted": True,
    }


def test_normalize_config_spec_missing_name_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({})


def test_normalize_config_spec_non_str_name_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": 1})


def test_normalize_config_spec_non_list_secrets_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": "gateway-tls", "secrets": "cert"})


def test_normalize_config_spec_non_str_secret_entry_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": "gateway-tls", "secrets": [1]})


def test_normalize_config_spec_empty_str_secret_entry_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": "gateway-tls", "secrets": [""]})


def test_normalize_config_spec_non_dict_files_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": "gateway-tls", "files": ["a"]})


def test_normalize_config_spec_non_int_rotation_days_raises() -> None:
    with pytest.raises(config_object.ConfigSpecError):
        config_object.normalize_config_spec({"name": "gateway-tls", "rotationDays": "90"})


def _by_type(conds):
    return {c["type"]: c for c in conds}


def test_config_conditions_secret_present_true_when_all_present() -> None:
    spec = config_object.normalize_config_spec(
        {"name": "gateway-tls", "secrets": ["gateway-tls-cert"]}
    )
    observed = {"present_secrets": ["gateway-tls-cert"]}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["SecretPresent"]["status"] == "True"


def test_config_conditions_secret_present_false_when_missing() -> None:
    spec = config_object.normalize_config_spec(
        {"name": "gateway-tls", "secrets": ["gateway-tls-cert", "gateway-tls-key"]}
    )
    observed = {"present_secrets": ["gateway-tls-cert"]}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["SecretPresent"]["status"] == "False"


def test_config_conditions_drift_true_when_hash_differs() -> None:
    spec = config_object.normalize_config_spec(
        {"name": "gateway-tls", "files": {"/etc/gateway/tls.crt": "a" * 64}}
    )
    observed = {"file_hashes": {"/etc/gateway/tls.crt": "b" * 64}}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["ConfigDrift"]["status"] == "True"


def test_config_conditions_drift_false_when_hash_matches() -> None:
    spec = config_object.normalize_config_spec(
        {"name": "gateway-tls", "files": {"/etc/gateway/tls.crt": "a" * 64}}
    )
    observed = {"file_hashes": {"/etc/gateway/tls.crt": "a" * 64}}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["ConfigDrift"]["status"] == "False"


def test_config_conditions_rotation_overdue_true_past_window() -> None:
    spec = config_object.normalize_config_spec({"name": "gateway-tls", "rotationDays": 30})
    observed = {"oldest_secret_age_days": 45}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["RotationOverdue"]["status"] == "True"


def test_config_conditions_rotation_overdue_false_within_window() -> None:
    spec = config_object.normalize_config_spec({"name": "gateway-tls", "rotationDays": 30})
    observed = {"oldest_secret_age_days": 5}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["RotationOverdue"]["status"] == "False"


def test_config_conditions_rotation_overdue_false_when_not_set() -> None:
    spec = config_object.normalize_config_spec({"name": "gateway-tls"})
    observed = {"oldest_secret_age_days": 999}
    conds = _by_type(config_object.config_conditions(spec, observed, NOW))
    assert conds["RotationOverdue"]["status"] == "False"


def test_explain_registry_has_config_kind() -> None:
    assert "config" in explain()["kinds"]
    described = explain("config")
    assert described["kind"] == "Config"
    assert "SecretPresent" in described["conditions"]
    assert "ConfigDrift" in described["conditions"]
    assert "RotationOverdue" in described["conditions"]
    assert any("skfleet get configs" in a for a in described["actions"])
    assert any("skfleet describe config <name>" in a for a in described["actions"])
