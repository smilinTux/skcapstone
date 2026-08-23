"""Tests for the Agent kind: spec normalization and drift conditions."""

from __future__ import annotations

import pytest

from skcapstone.fleet import agent
from skcapstone.fleet.explain import explain

NOW = "2026-07-28T12:00:00Z"


def test_normalize_agent_spec_defaults() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina"})
    assert spec == {
        "name": "lumina",
        "soul": None,
        "model": None,
        "daemon": {},
        "deleted": False,
    }


def test_normalize_agent_spec_echoes_provided_fields() -> None:
    spec = agent.normalize_agent_spec(
        {
            "name": "lumina",
            "soul": "lumina-soul",
            "model": "opus",
            "daemon": {"node": "node-158"},
            "deleted": True,
        }
    )
    assert spec == {
        "name": "lumina",
        "soul": "lumina-soul",
        "model": "opus",
        "daemon": {"node": "node-158"},
        "deleted": True,
    }


def test_normalize_agent_spec_missing_name_raises() -> None:
    with pytest.raises(agent.AgentSpecError):
        agent.normalize_agent_spec({})


def test_normalize_agent_spec_non_str_name_raises() -> None:
    with pytest.raises(agent.AgentSpecError):
        agent.normalize_agent_spec({"name": 1})


def test_normalize_agent_spec_non_str_soul_raises() -> None:
    with pytest.raises(agent.AgentSpecError):
        agent.normalize_agent_spec({"name": "lumina", "soul": 1})


def test_normalize_agent_spec_non_str_model_raises() -> None:
    with pytest.raises(agent.AgentSpecError):
        agent.normalize_agent_spec({"name": "lumina", "model": 1})


def test_normalize_agent_spec_non_dict_daemon_raises() -> None:
    with pytest.raises(agent.AgentSpecError):
        agent.normalize_agent_spec({"name": "lumina", "daemon": "node-158"})


def _by_type(conds):
    return {c["type"]: c for c in conds}


def test_agent_conditions_in_sync_when_no_soul_or_model_set() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina"})
    observed = {"active_soul": None, "model": None, "daemon_ready": True}
    conds = _by_type(agent.agent_conditions(spec, observed, NOW))
    assert conds["SoulLoaded"]["status"] == "True"
    assert conds["ModelRoutable"]["status"] == "True"
    assert conds["DaemonReady"]["status"] == "True"


def test_agent_conditions_in_sync_with_soul_and_model_set() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina", "soul": "lumina-soul", "model": "opus"})
    observed = {"active_soul": "lumina-soul", "model": "opus", "daemon_ready": True}
    conds = _by_type(agent.agent_conditions(spec, observed, NOW))
    assert conds["SoulLoaded"]["status"] == "True"
    assert conds["ModelRoutable"]["status"] == "True"
    assert conds["DaemonReady"]["status"] == "True"


def test_agent_conditions_soul_mismatch() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina", "soul": "lumina-soul"})
    observed = {"active_soul": "other-soul", "daemon_ready": True}
    conds = _by_type(agent.agent_conditions(spec, observed, NOW))
    assert conds["SoulLoaded"]["status"] == "False"


def test_agent_conditions_model_mismatch() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina", "model": "opus"})
    observed = {"model": "haiku", "daemon_ready": True}
    conds = _by_type(agent.agent_conditions(spec, observed, NOW))
    assert conds["ModelRoutable"]["status"] == "False"


def test_agent_conditions_daemon_not_ready() -> None:
    spec = agent.normalize_agent_spec({"name": "lumina"})
    observed = {"daemon_ready": False}
    conds = _by_type(agent.agent_conditions(spec, observed, NOW))
    assert conds["DaemonReady"]["status"] == "False"


def test_explain_registry_has_agent_kind() -> None:
    assert "agent" in explain()["kinds"]
    described = explain("agent")
    assert described["kind"] == "Agent"
    assert "SoulLoaded" in described["conditions"]
    assert "ModelRoutable" in described["conditions"]
    assert "DaemonReady" in described["conditions"]
    assert any("skfleet get agents" in a for a in described["actions"])
    assert any("skfleet describe agent" in a for a in described["actions"])
