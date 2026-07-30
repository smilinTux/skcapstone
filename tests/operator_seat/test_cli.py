"""The skoperator CLI: status, freeze/unfreeze (human), pending, decide."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import cli, decisions


def _enroll(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None},
    )
    paths = FleetPaths(root=tmp_path / "fleet")
    op = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=op)
    sknoded.run_once(paths, "node-158")
    return paths


def test_status_and_freeze_cycle(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    r = CliRunner()
    assert "active" in r.invoke(cli.operator, ["status"]).output
    assert r.invoke(cli.operator, ["freeze", "--reason", "drill"]).exit_code == 0
    assert "FROZEN" in r.invoke(cli.operator, ["status"]).output
    assert r.invoke(cli.operator, ["unfreeze"]).exit_code == 0
    assert "active" in r.invoke(cli.operator, ["status"]).output


def test_pending_and_decide(tmp_path, monkeypatch):
    paths = _enroll(tmp_path, monkeypatch)
    ddir = str(paths.root / "decisions")
    decisions.park(
        ddir,
        [{"action": "delete_object", "object": "x"}],
        decision_id="d1",
        created_iso="2026-07-29T00:00:00Z",
    )
    r = CliRunner()
    assert "d1" in r.invoke(cli.operator, ["pending"]).output
    out = r.invoke(cli.operator, ["decide", "d1", "--approve", "--choice", "0"])
    assert out.exit_code == 0 and "approved" in out.output
    assert decisions.list_pending(ddir) == []  # resolved, no longer pending


def test_pending_empty(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    out = CliRunner().invoke(cli.operator, ["pending"])
    assert "no pending decisions" in out.output


def test_apps_register_list_and_ratify(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    r = CliRunner()

    # Nothing registered yet.
    assert "no registered apps" in r.invoke(cli.operator, ["apps", "list"]).output

    # Register all six app adapters.
    reg = r.invoke(cli.operator, ["apps", "register"])
    assert reg.exit_code == 0
    assert "skchat" in reg.output and "skcode" in reg.output

    # List shows them, none ratified yet.
    listed = r.invoke(cli.operator, ["apps", "list"]).output
    assert "skchat" in listed
    assert "0/2 ratified" in listed  # skchat proposes 2, none ratified

    # Human ratifies one.
    rat = r.invoke(cli.operator, ["apps", "ratify", "skchat", "restart-daemon"])
    assert rat.exit_code == 0 and "auto-standard" in rat.output

    # Ratifying an unproposed action is a clean error, not a crash.
    bad = r.invoke(cli.operator, ["apps", "ratify", "skchat", "purge-outbox"])
    assert bad.exit_code != 0
