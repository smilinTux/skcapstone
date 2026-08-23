"""Tests for ConfigController: read-time Config rows."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import config_controller, store
from skcapstone.fleet.cli import fleet

NOW = "2026-07-28T12:00:00Z"


@pytest.fixture
def noded41():
    from skcapstone.fleet.store import Writer

    return Writer(role="sknoded", node="node-41", identity="")


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def _config(paths, operator, name="gateway-tls", **spec_kw) -> dict:
    spec = {"name": name}
    spec.update(spec_kw)
    return store.write_spec(paths, "config", name, spec, writer=operator)


def _observe(paths, noded41, name, observed: dict) -> None:
    store.write_status(
        paths,
        "config",
        name,
        node="node-41",
        status={"observed": observed},
        conditions=[],
        observed_generation=1,
        writer=noded41,
    )


def test_config_rows_all_present_no_drift_not_overdue(paths, operator, noded41) -> None:
    _config(
        paths,
        operator,
        "gateway-tls",
        secrets=["gateway-tls-cert"],
        files={"/etc/gateway/tls.crt": "a" * 64},
        rotationDays=30,
    )
    _observe(
        paths,
        noded41,
        "gateway-tls",
        {
            "present_secrets": ["gateway-tls-cert"],
            "file_hashes": {"/etc/gateway/tls.crt": "a" * 64},
            "oldest_secret_age_days": 5,
        },
    )
    rows = {r.name: r for r in config_controller.config_rows(paths, NOW)}
    row = rows["gateway-tls"]
    assert row.node == "node-41"
    assert row.secrets_present is True
    assert row.drift is False
    assert row.rotation_overdue is False


def test_config_rows_missing_secret(paths, operator, noded41) -> None:
    _config(paths, operator, "gateway-tls", secrets=["gateway-tls-cert", "gateway-tls-key"])
    _observe(paths, noded41, "gateway-tls", {"present_secrets": ["gateway-tls-cert"]})
    row = config_controller.config_rows(paths, NOW)[0]
    assert row.secrets_present is False


def test_config_rows_drifted(paths, operator, noded41) -> None:
    _config(paths, operator, "gateway-tls", files={"/etc/gateway/tls.crt": "a" * 64})
    _observe(paths, noded41, "gateway-tls", {"file_hashes": {"/etc/gateway/tls.crt": "b" * 64}})
    row = config_controller.config_rows(paths, NOW)[0]
    assert row.drift is True


def test_config_rows_rotation_overdue(paths, operator, noded41) -> None:
    _config(paths, operator, "gateway-tls", rotationDays=30)
    _observe(paths, noded41, "gateway-tls", {"oldest_secret_age_days": 45})
    row = config_controller.config_rows(paths, NOW)[0]
    assert row.rotation_overdue is True


def test_config_rows_missing_observed_defaults(paths, operator) -> None:
    _config(
        paths,
        operator,
        "gateway-tls",
        secrets=["gateway-tls-cert"],
        files={"/etc/gateway/tls.crt": "a" * 64},
        rotationDays=30,
    )
    row = config_controller.config_rows(paths, NOW)[0]
    assert row.node is None
    assert row.secrets_present is False
    assert row.drift is True
    assert row.rotation_overdue is False


def test_config_rows_skips_deleted(paths, operator) -> None:
    _config(paths, operator, "gateway-tls")
    _config(paths, operator, "gone", deleted=True)
    rows = {r.name for r in config_controller.config_rows(paths, NOW)}
    assert rows == {"gateway-tls"}


def test_cli_get_configs_lists_columns(paths, operator, noded41) -> None:
    _config(paths, operator, "gateway-tls", secrets=["gateway-tls-cert"])
    _observe(paths, noded41, "gateway-tls", {"present_secrets": ["gateway-tls-cert"]})
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "configs"], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "NAME" in out.output and "SECRETS" in out.output and "DRIFT" in out.output
    assert "ROTATION" in out.output
    assert "gateway-tls" in out.output and "node-41" in out.output


def test_cli_get_configs_empty(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["get", "configs"], env=_env(paths))
    assert out.exit_code == 0
    assert "no configs" in out.output


def test_cli_describe_config(paths, operator) -> None:
    _config(paths, operator, "gateway-tls", secrets=["gateway-tls-cert"])
    runner = CliRunner()
    out = runner.invoke(fleet, ["describe", "config", "gateway-tls"], env=_env(paths))
    assert out.exit_code == 0, out.output
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "gateway-tls"
    assert payload["spec"]["spec"]["secrets"] == ["gateway-tls-cert"]
