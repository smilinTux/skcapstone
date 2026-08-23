"""Tests for the fleet tree layout helpers."""

from __future__ import annotations

from pathlib import Path

from skcapstone.fleet.paths import FleetPaths, default_paths, self_node_name, valid_name


def test_tree_layout(paths: FleetPaths) -> None:
    root = paths.root
    assert paths.spec_path("node", "node-41") == root / "objects" / "node" / "node-41.json"
    assert (
        paths.placement_path("service", "skgateway")
        == root / "placements" / "service" / "skgateway.json"
    )
    assert (
        paths.status_path("node-41", "service", "skgateway")
        == root / "status" / "node-41" / "service" / "skgateway.json"
    )
    assert paths.heartbeat_path("node-41") == root / "status" / "node-41" / "heartbeat.json"
    assert paths.node_report_path("node-41") == root / "status" / "node-41" / "node.json"
    assert paths.join_path("node-41") == root / "status" / "node-41" / "join.json"
    assert paths.events_path("node-41") == root / "status" / "node-41" / "events.jsonl"
    assert paths.freeze_path() == root / "objects" / "_freeze.json"


def test_default_paths_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "elsewhere"))
    assert default_paths().root == tmp_path / "elsewhere"
    monkeypatch.delenv("SKFLEET_ROOT")
    assert default_paths().root == Path("~/.skcapstone/fleet").expanduser()


def test_self_node_name(monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    assert self_node_name() == "node-test"
    monkeypatch.delenv("SKFLEET_NODE")
    name = self_node_name()
    assert name.startswith("node-") and valid_name(name)


def test_valid_name_rejects_traversal() -> None:
    assert valid_name("skgateway")
    assert valid_name("node-41")
    assert not valid_name("../evil")
    assert not valid_name("a/b")
    assert not valid_name("")
    assert not valid_name("_freeze")
