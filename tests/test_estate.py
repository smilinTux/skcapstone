"""Contract tests for the estate-wide / host-local configuration split."""

import json
from pathlib import Path

import pytest

from skcapstone.estate import (
    EstateConfigError,
    host_lifecycle_claim,
    load_estate_profile,
    local_host,
    sovereign_home,
)


def write_estate(home: Path, **fields: object) -> Path:
    path = home / "config/estate.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "sk.estate-authority/v1", "operator": "chef", "realm": "skworld.io"}
    payload.update(fields)
    path.write_text(json.dumps(payload))
    return path


def test_estate_record_supplies_operator_realm_and_scope(tmp_path: Path) -> None:
    write_estate(tmp_path, operator="Casey", product_scope=["skcapstone", "skworld"])
    profile = load_estate_profile(tmp_path)
    assert profile.operator == "casey"
    assert profile.realm == "skworld.io"
    assert profile.product_scope == {"skcapstone", "skworld"}


def test_estate_record_defaults_scope_to_the_shipped_product_scope(tmp_path: Path) -> None:
    write_estate(tmp_path)
    assert load_estate_profile(tmp_path).product_scope == {
        "skcapstone",
        "skdashboard",
        "skworld",
    }


def test_cluster_json_is_the_fallback_operator_source(tmp_path: Path) -> None:
    (tmp_path / "cluster.json").write_text(json.dumps({"operator": "chef", "realm": "skworld.io"}))
    profile = load_estate_profile(tmp_path)
    assert profile.operator == "chef"
    assert profile.source.endswith("cluster.json")


def test_missing_estate_configuration_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.estate.SYSTEM_CLUSTER_PATH", tmp_path / "absent.json")
    with pytest.raises(EstateConfigError, match="no estate authority record"):
        load_estate_profile(tmp_path)


@pytest.mark.parametrize("operator", ["", "   ", "Casey Jones", "../etc"])
def test_malformed_operator_fails_closed(tmp_path: Path, operator: str) -> None:
    write_estate(tmp_path, operator=operator)
    with pytest.raises(EstateConfigError, match="operator is missing or malformed"):
        load_estate_profile(tmp_path)


def test_wrong_estate_schema_fails_closed(tmp_path: Path) -> None:
    write_estate(tmp_path, schema="sk.estate-authority/v99")
    with pytest.raises(EstateConfigError, match="schema must be"):
        load_estate_profile(tmp_path)


def test_sovereign_home_respects_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "estate"))
    assert sovereign_home() == tmp_path / "estate"
    assert sovereign_home(tmp_path / "other") == tmp_path / "other"


def test_no_host_claim_when_the_machine_makes_none(tmp_path: Path) -> None:
    assert host_lifecycle_claim(config_home=tmp_path, host="noroc2027") is None


def test_host_may_only_claim_itself(tmp_path: Path) -> None:
    """A claim copied to a second node refuses rather than promoting it."""
    path = tmp_path / "skcapstone/lifecycle-host.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "sk.lifecycle-host/v1", "active_host": "noroc2027"}))
    assert host_lifecycle_claim(config_home=tmp_path, host="noroc2027") == "noroc2027"
    with pytest.raises(EstateConfigError, match="this machine is chiap08"):
        host_lifecycle_claim(config_home=tmp_path, host="chiap08")


def test_malformed_host_claim_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "skcapstone/lifecycle-host.json"
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    with pytest.raises(EstateConfigError, match="malformed"):
        host_lifecycle_claim(config_home=tmp_path, host="noroc2027")
    path.write_text(json.dumps({"schema": "wrong", "active_host": "noroc2027"}))
    with pytest.raises(EstateConfigError, match="schema must be"):
        host_lifecycle_claim(config_home=tmp_path, host="noroc2027")


def test_the_gate_reads_the_machine_not_an_environment_variable(monkeypatch) -> None:
    """local_host() is the only host answer the gate trusts."""
    monkeypatch.setenv("SKFLEET_NODE", "node-somewhere-else")
    monkeypatch.setattr("skcapstone.estate.socket.gethostname", lambda: "NorOC2027.lan")
    assert local_host() == "noroc2027"
