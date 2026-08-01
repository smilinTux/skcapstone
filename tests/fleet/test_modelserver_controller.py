"""Tests for ModelController: read-time ModelServer rows."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import modelserver_controller, store
from skcapstone.fleet.cli import fleet

NOW = "2026-07-28T12:00:00Z"


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def _modelserver(paths, operator, name="vllm-0", **spec_kw) -> dict:
    spec = {"name": name, "ports": [8000], "models": ["opus"], "node": "node-41"}
    spec.update(spec_kw)
    return store.write_spec(paths, "modelserver", name, spec, writer=operator)


def test_modelserver_rows_serving_true_when_observed_matches(paths, operator, noded41) -> None:
    _modelserver(paths, operator, "vllm-0")
    store.write_status(
        paths,
        "modelserver",
        "vllm-0",
        node="node-41",
        status={"open_ports": [8000], "loaded_models": ["opus"], "vram_gb": 24},
        conditions=[],
        observed_generation=1,
        writer=noded41,
    )
    rows = {r.name: r for r in modelserver_controller.modelserver_rows(paths, NOW)}
    row = rows["vllm-0"]
    assert row.node == "node-41"
    assert row.ports == [8000]
    assert row.serving is True
    assert row.vram == 24


def test_modelserver_rows_serving_false_when_ports_not_open(paths, operator, noded41) -> None:
    _modelserver(paths, operator, "vllm-0")
    store.write_status(
        paths,
        "modelserver",
        "vllm-0",
        node="node-41",
        status={"open_ports": [], "loaded_models": ["opus"], "vram_gb": 24},
        conditions=[],
        observed_generation=1,
        writer=noded41,
    )
    row = modelserver_controller.modelserver_rows(paths, NOW)[0]
    assert row.serving is False
    assert row.vram == 24


def test_modelserver_rows_missing_observed_defaults_to_not_serving(paths, operator) -> None:
    _modelserver(paths, operator, "vllm-0")
    row = modelserver_controller.modelserver_rows(paths, NOW)[0]
    assert row.serving is False
    assert row.vram is None


def test_modelserver_rows_skips_deleted(paths, operator) -> None:
    _modelserver(paths, operator, "vllm-0")
    _modelserver(paths, operator, "gone", deleted=True)
    rows = {r.name: r for r in modelserver_controller.modelserver_rows(paths, NOW)}
    assert set(rows) == {"vllm-0"}


def test_cli_get_modelservers_lists_columns(paths, operator) -> None:
    _modelserver(paths, operator, "vllm-0")
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "modelservers"], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "NAME" in out.output and "SERVING" in out.output and "VRAM" in out.output
    assert "vllm-0" in out.output and "node-41" in out.output


def test_cli_get_modelservers_empty(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "modelservers"], env=_env(paths))
    assert out.exit_code == 0
    assert "no modelservers" in out.output


def test_cli_describe_modelserver(paths, operator) -> None:
    _modelserver(paths, operator, "vllm-0")
    runner = CliRunner()
    out = runner.invoke(fleet, ["describe", "modelserver", "vllm-0"], env=_env(paths))
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "vllm-0"
    assert payload["spec"]["spec"]["node"] == "node-41"
