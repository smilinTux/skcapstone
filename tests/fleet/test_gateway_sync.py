"""Tests for gateway_upstream_health: pure ModelServerRow to health-payload mapping."""

from __future__ import annotations

from skcapstone.fleet import gateway_sync
from skcapstone.fleet.modelserver_controller import ModelServerRow


def _row(name="vllm-0", **kw) -> ModelServerRow:
    defaults = {"node": "node-41", "ports": [8000], "serving": True, "vram": 24}
    defaults.update(kw)
    return ModelServerRow(name=name, **defaults)


def test_serving_row_maps_serving_true() -> None:
    health = gateway_sync.gateway_upstream_health([_row(serving=True)])
    assert health["vllm-0"]["serving"] is True


def test_not_serving_row_maps_serving_false() -> None:
    health = gateway_sync.gateway_upstream_health([_row(serving=False)])
    assert health["vllm-0"]["serving"] is False


def test_ports_vram_node_carried_through() -> None:
    health = gateway_sync.gateway_upstream_health(
        [_row(ports=[8000, 8001], vram=24.0, node="node-41")]
    )
    entry = health["vllm-0"]
    assert entry["ports"] == [8000, 8001]
    assert entry["vram"] == 24.0
    assert entry["node"] == "node-41"


def test_empty_input_returns_empty_dict() -> None:
    assert gateway_sync.gateway_upstream_health([]) == {}


def test_multiple_rows_keyed_by_name() -> None:
    health = gateway_sync.gateway_upstream_health(
        [_row(name="vllm-0"), _row(name="vllm-1", serving=False)]
    )
    assert set(health) == {"vllm-0", "vllm-1"}
    assert health["vllm-0"]["serving"] is True
    assert health["vllm-1"]["serving"] is False
