"""Tests for allocatable headroom (capacity minus reserves, spec 5.1)."""

from __future__ import annotations

from skcapstone.fleet import capacity, node_controller, sknoded, store

CAP = {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None}


def test_allocatable_subtracts_reserves() -> None:
    assert capacity.allocatable(CAP) == {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0}


def test_allocatable_floors() -> None:
    tiny = {"cores": 1, "ram_gb": 0.5, "disk_gb": 2.0, "gpu": None, "vram_gb": None}
    assert capacity.allocatable(tiny) == {"cores": 1, "ram_gb": 0.0, "disk_gb": 0.0}


def test_node_report_carries_allocatable(paths, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")
    report = store.read_node_file(paths, "node-41", "node.json")
    assert report["status"]["allocatable"] == {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0}


def test_node_view_allocatable_with_capacity_fallback(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    view = {v.name: v for v in node_controller.node_views(paths)}["node-41"]
    assert view.allocatable == {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0}
    # a pre-Phase-2 node.json (no allocatable key) falls back to capacity
    noded_old = store.Writer(role="sknoded", node="node-old", identity="")
    store.write_node_file(
        paths,
        noded_old,
        "node.json",
        {
            "kind": "Node",
            "name": "node-old",
            "node": "node-old",
            "observedGeneration": 1,
            "conditions": [],
            "status": {"capacity": {"cores": 2, "ram_gb": 4.0, "disk_gb": 10.0}},
        },
    )
    store.write_spec(paths, "node", "node-old", {}, writer=operator)
    view = {v.name: v for v in node_controller.node_views(paths)}["node-old"]
    assert view.allocatable == {"cores": 2, "ram_gb": 4.0, "disk_gb": 10.0}
