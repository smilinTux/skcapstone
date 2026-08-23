"""Tests for NodeController phase derivation and cordon."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skcapstone.fleet import node_controller, store

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _beat(paths, node: str, age_s: float) -> None:
    writer = store.Writer(role="sknoded", node=node, identity="")
    ts = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.write_node_file(
        paths,
        writer,
        "heartbeat.json",
        {"kind": "Node", "name": node, "node": node, "ts": ts},
        if_changed=False,
    )


def _admit(paths, operator, node: str) -> None:
    store.write_spec(
        paths,
        "node",
        node,
        {"cordoned": False, "taints": []},
        writer=operator,
        labels={"tier": "test"},
    )


def test_phases_from_heartbeat_age(paths, operator) -> None:
    for node, age in [("node-a", 10), ("node-b", 200), ("node-c", 400)]:
        _admit(paths, operator, node)
        _beat(paths, node, age)
    views = {v.name: v for v in node_controller.node_views(paths, now=NOW)}
    assert views["node-a"].phase == "Ready"
    assert views["node-b"].phase == "NotReady"
    assert views["node-c"].phase == "Dead"
    assert views["node-a"].heartbeat_age_s == 10.0


def test_never_beaten_is_dead_and_join_is_pending(paths, operator) -> None:
    _admit(paths, operator, "node-silent")
    joiner = store.Writer(role="sknoded", node="node-new", identity="")
    store.write_node_file(paths, joiner, "join.json", {"name": "node-new"}, if_changed=False)
    views = {v.name: v for v in node_controller.node_views(paths, now=NOW)}
    assert views["node-silent"].phase == "Dead"
    assert views["node-new"].phase == "Pending"


def test_phase_boundaries_at_180_and_300(paths, operator) -> None:
    _admit(paths, operator, "node-edge")
    _beat(paths, "node-edge", 180.0)
    assert node_controller._phase(180.0) == "Ready"
    _beat(paths, "node-edge", 180.001)
    assert node_controller._phase(180.001) == "NotReady"
    _beat(paths, "node-edge", 300.0)
    assert node_controller._phase(300.0) == "NotReady"
    _beat(paths, "node-edge", 300.001)
    assert node_controller._phase(300.001) == "Dead"


def test_cordon_round_trip(paths, operator) -> None:
    _admit(paths, operator, "node-a")
    _beat(paths, "node-a", 10)
    updated = node_controller.cordon(paths, "node-a", True, writer=operator)
    assert updated["spec"]["cordoned"] is True
    assert updated["generation"] == 2
    view = {v.name: v for v in node_controller.node_views(paths, now=NOW)}["node-a"]
    assert view.cordoned is True
    node_controller.cordon(paths, "node-a", False, writer=operator)
    assert store.read_spec(paths, "node", "node-a")["spec"]["cordoned"] is False
