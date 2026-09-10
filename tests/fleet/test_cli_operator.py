"""Tests for the `skfleet operator explain/observe/act` CLI (Seat O3b)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.cli import fleet
from skcapstone.operator_seat import adapter as operator_adapter


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None},
    )


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


def _enroll(paths):
    operator = store.Writer(role="operator", node="node-cli", identity="capauth:chef@skworld.io")
    sknoded.run_once(paths, "node-cli")
    store.write_spec(paths, "node", "node-cli", {"cordoned": False}, writer=operator)
    sknoded.run_once(paths, "node-cli")
    return operator


def test_cli_operator_explain_is_contract_conformant(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["operator", "explain", "--json"], env=_env(paths))
    assert out.exit_code == 0
    payload = json.loads(out.output)
    assert operator_adapter.validate_explain(payload) == []


def test_cli_operator_observe_is_contract_conformant(paths) -> None:
    _enroll(paths)
    runner = CliRunner()
    out = runner.invoke(fleet, ["operator", "observe", "--json"], env=_env(paths))
    assert out.exit_code == 0
    payload = json.loads(out.output)
    assert operator_adapter.validate_observe(payload) == []


def test_cli_operator_act_is_idempotent_by_change_id(paths) -> None:
    operator = _enroll(paths)
    store.write_spec(
        paths, "cronjob", "nightly", {"schedule": "@daily", "command": "echo hi"}, writer=operator
    )
    runner = CliRunner()
    args = [
        "operator",
        "act",
        "rerun_cronjob",
        "--object",
        "nightly",
        "--change-id",
        "chg-1",
        "--json",
    ]
    first = runner.invoke(fleet, args, env=_env(paths))
    assert first.exit_code == 0
    first_payload = json.loads(first.output)
    assert first_payload["applied"] is True
    assert first_payload["already_applied"] is False

    log = store.read_spec(paths, "cronjob", "nightly")["spec"]["operatorActions"]
    assert len(log) == 1

    second = runner.invoke(fleet, args, env=_env(paths))
    assert second.exit_code == 0
    second_payload = json.loads(second.output)
    assert second_payload["already_applied"] is True

    log_after = store.read_spec(paths, "cronjob", "nightly")["spec"]["operatorActions"]
    assert len(log_after) == 1  # no duplicate write on repeat change-id


def test_cli_operator_act_honors_freeze(paths) -> None:
    operator = _enroll(paths)
    store.write_spec(
        paths, "cronjob", "nightly", {"schedule": "@daily", "command": "echo hi"}, writer=operator
    )
    human = store.Writer(role="operator", node="node-cli", identity="chef")
    store.set_frozen(paths, True, writer=human)
    runner = CliRunner()
    out = runner.invoke(
        fleet,
        ["operator", "act", "rerun_cronjob", "--object", "nightly", "--change-id", "chg-2"],
        env=_env(paths),
    )
    assert out.exit_code != 0
    assert "frozen" in out.output

    log = store.read_spec(paths, "cronjob", "nightly")["spec"].get("operatorActions", [])
    assert log == []  # refused: nothing recorded while frozen


def test_cli_operator_act_restart_service_calls_actuation(paths, monkeypatch) -> None:
    operator = _enroll(paths)
    store.write_spec(paths, "service", "web", {"unit": "web.service"}, writer=operator)
    calls = []

    def fake_runner(cmd):
        import subprocess

        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("skcapstone.fleet.actuation.default_runner", fake_runner)
    runner = CliRunner()
    out = runner.invoke(
        fleet,
        ["operator", "act", "restart_service", "--object", "web", "--change-id", "chg-3", "--json"],
        env=_env(paths),
    )
    assert out.exit_code == 0
    payload = json.loads(out.output)
    assert payload["actuation"]["performed"] is True
    assert any("web.service" in c for c in calls)
