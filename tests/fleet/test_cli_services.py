"""Tests for skfleet apply/services/reconcile/actuation + pilot spec docs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.fleet import events, node_controller, services, store
from skcapstone.fleet.cli import fleet

PILOT_DIR = Path(__file__).resolve().parents[2] / "docs" / "fleet" / "pilot-services"
PILOTS = ["skwhisper-lumina", "skgateway", "skcomms", "skchat-daemon"]


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"}


def test_apply_writes_and_validates(paths, tmp_path) -> None:
    runner = CliRunner()
    doc = tmp_path / "svc.json"
    doc.write_text(
        json.dumps(
            {
                "kind": "service",
                "name": "skgateway",
                "labels": {"tier": "core"},
                "spec": {"unit": "skgateway.service"},
            }
        )
    )
    out = runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths))
    assert out.exit_code == 0, out.output
    assert "service/skgateway" in out.output and "generation 1" in out.output
    assert store.read_spec(paths, "service", "skgateway")["labels"] == {"tier": "core"}
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"kind": "service", "name": "x", "spec": {"runtime": "kubelet", "unit": "u"}})
    )
    out = runner.invoke(fleet, ["apply", "-f", str(bad)], env=_env(paths))
    assert out.exit_code != 0 and "runtime" in out.output
    assert store.read_spec(paths, "service", "x") is None  # rejected: no write


def test_apply_rejects_malformed_docs(paths, tmp_path) -> None:
    runner = CliRunner()
    doc = tmp_path / "nokind.json"
    doc.write_text(json.dumps({"name": "x", "spec": {}}))
    assert runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths)).exit_code != 0
    doc.write_text("not json")
    assert runner.invoke(fleet, ["apply", "-f", str(doc)], env=_env(paths)).exit_code != 0


def test_services_table_and_reconcile(paths, operator, noded41) -> None:
    runner = CliRunner()
    store.write_spec(
        paths,
        "node",
        "node-41",
        {"cordoned": False},
        writer=operator,
        labels={"always-on": "true"},
    )
    hb = store.Writer(role="sknoded", node="node-41", identity="")
    store.write_node_file(
        paths,
        hb,
        "heartbeat.json",
        {"kind": "Node", "name": "node-41", "node": "node-41", "ts": "2026-07-28T00:00:00Z"},
        if_changed=False,
    )
    store.write_node_file(
        paths,
        hb,
        "node.json",
        {
            "kind": "Node",
            "name": "node-41",
            "node": "node-41",
            "observedGeneration": 1,
            "conditions": [],
            "status": {
                "capacity": {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0},
                "allocatable": {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0},
            },
        },
    )
    store.write_spec(
        paths,
        "service",
        "skgateway",
        {"unit": "skgateway.service", "nodeSelector": {"always-on": "true"}},
        writer=operator,
    )
    out = runner.invoke(fleet, ["services"], env=_env(paths))
    assert "skgateway" in out.output and "unplaced" in out.output
    # reconcile places it; the heartbeat above is stale so node-41 is Dead,
    # therefore nothing can be placed yet and manual failover logic stays quiet
    out = runner.invoke(fleet, ["reconcile"], env=_env(paths))
    assert out.exit_code == 0
    assert "placed=0" in out.output


def test_actuation_toggle_round_trip(paths, operator) -> None:
    runner = CliRunner()
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    out = runner.invoke(fleet, ["actuation", "node-41", "--enable"], env=_env(paths))
    assert out.exit_code == 0
    assert store.read_spec(paths, "node", "node-41")["spec"]["actuate"] is True
    out = runner.invoke(fleet, ["actuation", "node-41", "--disable"], env=_env(paths))
    assert store.read_spec(paths, "node", "node-41")["spec"]["actuate"] is False
    out = runner.invoke(fleet, ["actuation", "missing-node", "--enable"], env=_env(paths))
    assert out.exit_code != 0


def test_set_actuation_preserves_other_spec_fields(paths, operator) -> None:
    store.write_spec(
        paths,
        "node",
        "node-41",
        {"cordoned": True, "taints": [{"key": "travel"}]},
        writer=operator,
        labels={"heavy-build": "true"},
    )
    node_controller.set_actuation(paths, "node-41", True, writer=operator)
    spec = store.read_spec(paths, "node", "node-41")
    assert spec["spec"]["cordoned"] is True
    assert spec["spec"]["taints"] == [{"key": "travel"}]
    assert spec["labels"] == {"heavy-build": "true"}
    assert spec["spec"]["actuate"] is True


def test_pilot_docs_are_valid_and_schedulable() -> None:
    assert sorted(p.stem for p in PILOT_DIR.glob("*.json")) == sorted(PILOTS)
    for path in PILOT_DIR.glob("*.json"):
        doc = json.loads(path.read_text())
        assert doc["kind"] == "service" and doc["name"] == path.stem
        spec = services.normalize_service_spec(doc["spec"])
        assert spec["failover"] == "manual"  # pilot set: conservative
        wl = services.service_workload(doc)
        assert wl.node_selector == {"always-on": "true"}
