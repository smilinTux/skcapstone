"""Tests for the Agent convergence plan (Phase 5, step 2a: pure planning)."""

from __future__ import annotations

from skcapstone.fleet import agent_convergence
from skcapstone.fleet.agent_convergence import ConvergeAction


def test_in_sync_spec_yields_empty_plan() -> None:
    spec = {
        "name": "lumina",
        "soul": "lumina-soul",
        "model": "opus",
        "daemon": {"node": "node-158"},
    }
    observed = {
        "active_soul": "lumina-soul",
        "model": "opus",
        "daemon_ready": True,
    }
    assert agent_convergence.agent_convergence_plan(spec, observed) == []


def test_soul_drift_yields_single_set_soul_action() -> None:
    spec = {"name": "lumina", "soul": "lumina-soul", "model": None, "daemon": {}}
    observed = {"active_soul": "other-soul"}
    plan = agent_convergence.agent_convergence_plan(spec, observed)
    assert plan == [
        ConvergeAction(kind="set_soul", target="lumina", detail={"soul": "lumina-soul"})
    ]


def test_model_drift_yields_set_model_action() -> None:
    spec = {"name": "lumina", "soul": None, "model": "opus", "daemon": {}}
    observed = {"model": "sonnet"}
    plan = agent_convergence.agent_convergence_plan(spec, observed)
    assert plan == [ConvergeAction(kind="set_model", target="lumina", detail={"model": "opus"})]


def test_daemon_not_ready_yields_ensure_daemon_action() -> None:
    spec = {"name": "lumina", "soul": None, "model": None, "daemon": {"node": "node-158"}}
    observed = {"daemon_ready": False}
    plan = agent_convergence.agent_convergence_plan(spec, observed)
    assert plan == [
        ConvergeAction(kind="ensure_daemon", target="lumina", detail={"node": "node-158"})
    ]


def test_multiple_simultaneous_drifts_yield_all_actions() -> None:
    spec = {
        "name": "lumina",
        "soul": "lumina-soul",
        "model": "opus",
        "daemon": {"node": "node-158"},
    }
    observed = {"active_soul": "other-soul", "model": "sonnet", "daemon_ready": False}
    plan = agent_convergence.agent_convergence_plan(spec, observed)
    assert plan == [
        ConvergeAction(kind="set_soul", target="lumina", detail={"soul": "lumina-soul"}),
        ConvergeAction(kind="set_model", target="lumina", detail={"model": "opus"}),
        ConvergeAction(kind="ensure_daemon", target="lumina", detail={"node": "node-158"}),
    ]


def test_unset_soul_and_model_omit_those_actions() -> None:
    spec = {"name": "lumina", "soul": None, "model": None, "daemon": {}}
    observed = {"active_soul": "some-soul", "model": "some-model"}
    assert agent_convergence.agent_convergence_plan(spec, observed) == []


def test_convergence_action_is_frozen() -> None:
    action = ConvergeAction(kind="set_soul", target="lumina", detail={"soul": "x"})
    try:
        action.kind = "set_model"
    except Exception as exc:
        assert "frozen" in str(exc).lower() or "cannot assign" in str(exc).lower()
    else:
        raise AssertionError("ConvergeAction should be immutable")
