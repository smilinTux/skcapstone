"""Tests for the ModelServer kind: spec normalization and Serving conditions."""

from __future__ import annotations

import pytest

from skcapstone.fleet import modelserver
from skcapstone.fleet.explain import explain

NOW = "2026-07-28T12:00:00Z"


def test_normalize_modelserver_spec_defaults() -> None:
    spec = modelserver.normalize_modelserver_spec({"name": "vllm-0"})
    assert spec == {
        "name": "vllm-0",
        "ports": [],
        "models": [],
        "node": None,
        "vramBudgetGb": None,
        "deleted": False,
    }


def test_normalize_modelserver_spec_echoes_provided_fields() -> None:
    spec = modelserver.normalize_modelserver_spec(
        {
            "name": "vllm-0",
            "ports": [8000, 8001],
            "models": ["opus", "haiku"],
            "node": "node-158",
            "vramBudgetGb": 24,
            "deleted": True,
        }
    )
    assert spec == {
        "name": "vllm-0",
        "ports": [8000, 8001],
        "models": ["opus", "haiku"],
        "node": "node-158",
        "vramBudgetGb": 24,
        "deleted": True,
    }


def test_normalize_modelserver_spec_missing_name_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({})


def test_normalize_modelserver_spec_non_str_name_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": 1})


def test_normalize_modelserver_spec_non_list_ports_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": "vllm-0", "ports": 8000})


def test_normalize_modelserver_spec_out_of_range_port_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": "vllm-0", "ports": [70000]})


def test_normalize_modelserver_spec_non_int_port_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": "vllm-0", "ports": ["8000"]})


def test_normalize_modelserver_spec_non_list_models_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": "vllm-0", "models": "opus"})


def test_normalize_modelserver_spec_non_str_node_raises() -> None:
    with pytest.raises(modelserver.ModelServerSpecError):
        modelserver.normalize_modelserver_spec({"name": "vllm-0", "node": 1})


def _by_type(conds):
    return {c["type"]: c for c in conds}


def test_modelserver_conditions_serving_true_when_ports_and_models_ready() -> None:
    spec = modelserver.normalize_modelserver_spec(
        {"name": "vllm-0", "ports": [8000], "models": ["opus"]}
    )
    observed = {"open_ports": [8000], "loaded_models": ["opus"], "vram_gb": 24}
    conds = _by_type(modelserver.modelserver_conditions(spec, observed, NOW))
    assert conds["Serving"]["status"] == "True"


def test_modelserver_conditions_serving_false_when_port_missing() -> None:
    spec = modelserver.normalize_modelserver_spec(
        {"name": "vllm-0", "ports": [8000, 8001], "models": ["opus"]}
    )
    observed = {"open_ports": [8000], "loaded_models": ["opus"], "vram_gb": 24}
    conds = _by_type(modelserver.modelserver_conditions(spec, observed, NOW))
    assert conds["Serving"]["status"] == "False"


def test_modelserver_conditions_serving_false_when_model_missing() -> None:
    spec = modelserver.normalize_modelserver_spec(
        {"name": "vllm-0", "ports": [8000], "models": ["opus", "haiku"]}
    )
    observed = {"open_ports": [8000], "loaded_models": ["opus"], "vram_gb": 24}
    conds = _by_type(modelserver.modelserver_conditions(spec, observed, NOW))
    assert conds["Serving"]["status"] == "False"


def test_explain_registry_has_modelserver_kind() -> None:
    assert "modelserver" in explain()["kinds"]
    described = explain("modelserver")
    assert described["kind"] == "ModelServer"
    assert "Serving" in described["conditions"]
    assert any("skfleet get modelservers" in a for a in described["actions"])
    assert any("skfleet describe modelserver" in a for a in described["actions"])
