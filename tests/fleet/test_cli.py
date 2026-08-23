"""Tests for the skfleet CLI surface."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.explain import explain


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None},
    )


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


def test_explain_registry() -> None:
    # Registered kinds accumulate as fleet phases land; assert membership rather
    # than an exact list so each new kind does not re-break this test.
    assert "node" in explain()["kinds"]
    node = explain("node")
    assert node["kind"] == "Node"
    assert "Ready" in node["conditions"]
    assert any("cordon" in a for a in node["actions"])


def test_cli_nodes_and_describe(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None},
    )
    sknoded.run_once(paths, "node-cli")
    store.write_spec(
        paths,
        "node",
        "node-cli",
        {"cordoned": False},
        writer=operator,
        labels={"interactive": "true"},
    )
    runner = CliRunner()
    out = runner.invoke(fleet, ["nodes"], env=_env(paths))
    assert out.exit_code == 0
    assert "node-cli" in out.output and "Ready" in out.output
    out = runner.invoke(fleet, ["describe", "node", "node-cli"], env=_env(paths))
    assert out.exit_code == 0
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "node-cli"


def test_cli_explain_json(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["explain", "node", "--json"], env=_env(paths))
    assert out.exit_code == 0
    assert json.loads(out.output)["kind"] == "Node"


def test_cli_cordon_and_freeze(paths, operator) -> None:
    store.write_spec(paths, "node", "node-cli", {"cordoned": False}, writer=operator)
    runner = CliRunner()
    assert runner.invoke(fleet, ["cordon", "node-cli"], env=_env(paths)).exit_code == 0
    assert store.read_spec(paths, "node", "node-cli")["spec"]["cordoned"] is True
    assert runner.invoke(fleet, ["uncordon", "node-cli"], env=_env(paths)).exit_code == 0
    assert runner.invoke(fleet, ["freeze", "--reason", "drill"], env=_env(paths)).exit_code == 0
    assert store.is_frozen(paths) is True
    assert runner.invoke(fleet, ["unfreeze"], env=_env(paths)).exit_code == 0
    assert store.is_frozen(paths) is False


def test_cli_admit_bootstrap_and_preset(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["admit", "node-158", "--bootstrap", "--preset"], env=_env(paths))
    assert out.exit_code == 0
    # `node-158` is now an ALIAS (card 8258517f): the preset is keyed by the
    # real node name, node-noroc2027, so this asserts the alias resolves and
    # the labels actually land rather than silently applying nothing.
    assert "admitted node-158 (generation 1, role=control)" in out.output
    spec = store.read_spec(paths, "node", "node-158")
    assert spec["labels"]["control-plane"] == "true"
    assert spec["spec"]["role"] == "control"


def test_cli_admit_with_explicit_labels(paths) -> None:
    sknoded.run_once(paths, "node-cli")
    runner = CliRunner()
    out = runner.invoke(fleet, ["admit", "node-cli", "--label", "role=edge"], env=_env(paths))
    assert out.exit_code == 0
    spec = store.read_spec(paths, "node", "node-cli")
    assert spec["labels"] == {"role": "edge"}


def test_cli_admit_requires_join_unless_bootstrap(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["admit", "node-ghost"], env=_env(paths))
    assert out.exit_code != 0
    assert "no join request" in out.output
