"""Tests for skfleet drain: cordon + resident listing + alert, no moves."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone.fleet import alerts, service_controller, store
from skcapstone.fleet.cli import fleet


def _populate(paths, operator, scheduler_writer, noded41) -> None:
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    store.write_spec(paths, "service", "skgateway", {"unit": "u1.service"}, writer=operator)
    store.write_spec(
        paths, "service", "coturn", {"unit": "coturn", "runtime": "docker"}, writer=operator
    )
    store.write_placement(
        paths, "service", "skgateway", node="node-41", reason="r", writer=scheduler_writer
    )
    store.write_placement(
        paths, "service", "coturn", node="node-158", reason="r", writer=scheduler_writer
    )
    # an observed-but-unplaced-here service (manual legacy resident)
    store.write_status(
        paths,
        "service",
        "legacy",
        node="node-41",
        status={
            "state": "active",
            "pid": 9,
            "since": "t",
            "restarts": 0,
            "runtime": "systemd-user",
        },
        conditions=[],
        observed_generation=0,
        writer=noded41,
    )


def test_node_residents_merges_placements_and_statuses(
    paths, operator, scheduler_writer, noded41
) -> None:
    _populate(paths, operator, scheduler_writer, noded41)
    residents = service_controller.node_residents(paths, "node-41")
    assert [(r["name"], r["via"]) for r in residents] == [
        ("legacy", "status"),
        ("skgateway", "placement"),
    ]
    assert residents[0]["state"] == "active"
    assert service_controller.node_residents(paths, "node-158") == [
        {"name": "coturn", "via": "placement", "state": "unobserved"}
    ]


def test_drain_cordons_lists_and_alerts(
    paths, operator, scheduler_writer, noded41, monkeypatch
) -> None:
    _populate(paths, operator, scheduler_writer, noded41)
    alerted: list[str] = []
    monkeypatch.setattr(alerts, "send_alert", lambda msg, **kw: alerted.append(msg) or True)
    runner = CliRunner()
    out = runner.invoke(
        fleet,
        ["drain", "node-41"],
        env={"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"},
    )
    assert out.exit_code == 0, out.output
    assert store.read_spec(paths, "node", "node-41")["spec"]["cordoned"] is True
    assert "skgateway" in out.output and "legacy" in out.output
    assert "manual move" in out.output.lower()
    assert alerted and "node-41" in alerted[0] and "skgateway" in alerted[0]
    # placements were NOT touched: drain never moves anything in v1
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"


def test_drain_unknown_node_fails_cleanly(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(
        fleet,
        ["drain", "node-nope"],
        env={"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-158"},
    )
    assert out.exit_code != 0 and "no such node" in out.output
