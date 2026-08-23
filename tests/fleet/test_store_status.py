"""Tests for status writes: ownership, write-on-change, merged read."""

from __future__ import annotations

import pytest

from skcapstone.fleet import store


def _write(paths, noded41, generation: int = 1, state: str = "active") -> bool:
    return store.write_status(
        paths,
        "service",
        "skgateway",
        node="node-41",
        status={"state": state},
        conditions=[
            {
                "type": "Ready",
                "status": "True",
                "reason": "UnitActive",
                "message": "ok",
                "lastTransition": "2026-07-27T00:00:00Z",
            }
        ],
        observed_generation=generation,
        writer=noded41,
    )


def test_status_ownership(paths, operator, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        _write(paths, operator)  # operator may not write status
    other = store.Writer(role="sknoded", node="node-158", identity="")
    with pytest.raises(store.OwnershipError):
        store.write_status(
            paths,
            "service",
            "skgateway",
            node="node-41",
            status={},
            conditions=[],
            observed_generation=1,
            writer=other,
        )  # a node never writes another node's subtree


def test_write_on_change(paths, noded41) -> None:
    assert _write(paths, noded41) is True
    assert _write(paths, noded41) is False  # identical: no write
    assert _write(paths, noded41, state="failed") is True


def test_node_file_guard_and_change_detection(paths, noded41) -> None:
    assert store.write_node_file(paths, noded41, "node.json", {"a": 1}) is True
    assert store.write_node_file(paths, noded41, "node.json", {"a": 1}) is False
    assert (
        store.write_node_file(paths, noded41, "heartbeat.json", {"ts": "x"}, if_changed=False)
        is True
    )
    with pytest.raises(store.OwnershipError):
        store.write_node_file(paths, noded41, "evil.json", {})
    assert store.read_node_file(paths, "node-41", "node.json")["a"] == 1
    written = paths.node_status_dir("node-41").rglob("*.json")
    assert all("node-41" in str(p) for p in written)


def test_merged_staleness(paths, operator, noded41) -> None:
    store.write_spec(paths, "service", "skgateway", {"unit": "skgateway.service"}, writer=operator)
    _write(paths, noded41, generation=1)
    m = store.merged(paths, "service", "skgateway")
    assert m["spec"]["generation"] == 1
    assert m["statuses"][0]["stale"] is False
    store.write_spec(
        paths,
        "service",
        "skgateway",
        {"unit": "skgateway.service", "paused": True},
        writer=operator,
    )
    m = store.merged(paths, "service", "skgateway")
    assert m["statuses"][0]["stale"] is True  # observedGeneration 1 < generation 2
    assert store.merged(paths, "service", "missing") is None
