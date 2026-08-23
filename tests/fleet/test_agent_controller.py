"""Tests for AgentController: read-time Agent rows."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import agent_controller, store
from skcapstone.fleet.cli import fleet

NOW = "2026-07-28T12:00:00Z"


@pytest.fixture
def noded41():
    from skcapstone.fleet.store import Writer

    return Writer(role="sknoded", node="node-41", identity="")


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def _agent(paths, operator, name="lumina", **spec_kw) -> dict:
    spec = {"name": name}
    spec.update(spec_kw)
    return store.write_spec(paths, "agent", name, spec, writer=operator)


def _observe(paths, noded41, name, observed: dict) -> None:
    store.write_status(
        paths,
        "agent",
        name,
        node="node-41",
        status={"observed": observed},
        conditions=[],
        observed_generation=1,
        writer=noded41,
    )


def test_agent_rows_in_sync(paths, operator, noded41) -> None:
    _agent(paths, operator, "lumina", soul="lumina-soul", model="opus")
    _observe(
        paths,
        noded41,
        "lumina",
        {"active_soul": "lumina-soul", "model": "opus", "daemon_ready": True},
    )
    rows = {r.name: r for r in agent_controller.agent_rows(paths, NOW)}
    row = rows["lumina"]
    assert row.node == "node-41"
    assert row.soul == "lumina-soul"
    assert row.model == "opus"
    assert row.ready == "True"


def test_agent_rows_drifted_soul_not_ready(paths, operator, noded41) -> None:
    _agent(paths, operator, "lumina", soul="lumina-soul", model="opus")
    _observe(
        paths,
        noded41,
        "lumina",
        {"active_soul": "other-soul", "model": "opus", "daemon_ready": True},
    )
    row = agent_controller.agent_rows(paths, NOW)[0]
    assert row.soul == "other-soul"
    assert row.ready == "False"


def test_agent_rows_missing_observed_defaults(paths, operator) -> None:
    _agent(paths, operator, "lumina", soul="lumina-soul", model="opus")
    row = agent_controller.agent_rows(paths, NOW)[0]
    assert row.node is None
    assert row.soul is None
    assert row.model is None
    assert row.ready == "False"


def test_agent_rows_ready_when_no_soul_or_model_set(paths, operator, noded41) -> None:
    _agent(paths, operator, "jarvis")
    _observe(paths, noded41, "jarvis", {"daemon_ready": True})
    row = agent_controller.agent_rows(paths, NOW)[0]
    assert row.ready == "True"


def test_agent_rows_skips_deleted(paths, operator) -> None:
    _agent(paths, operator, "lumina")
    _agent(paths, operator, "gone", deleted=True)
    rows = {r.name for r in agent_controller.agent_rows(paths, NOW)}
    assert rows == {"lumina"}


def test_cli_get_agents_lists_columns(paths, operator, noded41) -> None:
    _agent(paths, operator, "lumina", soul="lumina-soul", model="opus")
    _observe(
        paths,
        noded41,
        "lumina",
        {"active_soul": "lumina-soul", "model": "opus", "daemon_ready": True},
    )
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "agents"], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "NAME" in out.output and "SOUL" in out.output and "READY" in out.output
    assert "lumina" in out.output and "lumina-soul" in out.output and "True" in out.output


def test_cli_get_agents_empty(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "agents"], env=_env(paths))
    assert out.exit_code == 0
    assert "no agents" in out.output


def test_cli_describe_agent(paths, operator) -> None:
    _agent(paths, operator, "lumina", soul="lumina-soul")
    runner = CliRunner()
    out = runner.invoke(fleet, ["describe", "agent", "lumina"], env=_env(paths))
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "lumina"
    assert payload["spec"]["spec"]["soul"] == "lumina-soul"


def test_cli_get_unknown_resource(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "widgets"], env=_env(paths))
    assert out.exit_code != 0
    assert "unknown resource" in out.output
