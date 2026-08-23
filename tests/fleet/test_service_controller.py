"""Tests for ServiceController: place-once, manual failover, drift rows."""

from __future__ import annotations

import pytest

from skcapstone.fleet import events, service_controller, store
from skcapstone.fleet.node_controller import NodeView


@pytest.fixture(autouse=True)
def _fresh():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _views(dead41: bool = False) -> list[NodeView]:
    return [
        NodeView(
            name="node-158",
            phase="Ready",
            labels={"always-on": "true", "control-plane": "true"},
            allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0},
        ),
        NodeView(
            name="node-41",
            phase="Dead" if dead41 else "Ready",
            labels={"heavy-build": "true", "always-on": "true"},
            allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0},
        ),
    ]


def _svc(paths, operator, name="skgateway", **spec_kw) -> None:
    spec = {"unit": f"{name}.service", "nodeSelector": {"always-on": "true"}}
    spec.update(spec_kw)
    store.write_spec(paths, "service", name, spec, writer=operator)


def test_first_reconcile_places_unplaced_services(paths, operator) -> None:
    _svc(paths, operator)
    out = service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    assert out["placed"] == ["skgateway"]
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"


def test_place_once_never_moves_on_capacity_change(paths, operator) -> None:
    _svc(paths, operator)
    service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    flipped = [
        NodeView(
            name="node-158",
            phase="Ready",
            labels={"always-on": "true"},
            allocatable={"cores": 7, "ram_gb": 64.0, "disk_gb": 100.0},
        ),
        NodeView(
            name="node-41",
            phase="Ready",
            labels={"always-on": "true"},
            allocatable={"cores": 15, "ram_gb": 1.0, "disk_gb": 200.0},
        ),
    ]
    out = service_controller.reconcile_once(
        paths, node="node-158", views=flipped, alert=lambda *a, **k: True
    )
    assert out["kept"] == ["skgateway"] and out["placed"] == []
    placement = store.read_placement(paths, "service", "skgateway")
    assert placement["node"] == "node-41"  # unmoved
    assert placement["placementGeneration"] == 1  # zero churn


def test_manual_failover_alerts_and_never_replaces(paths, operator) -> None:
    _svc(paths, operator)  # failover defaults to manual
    service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    alerted: list[str] = []
    out = service_controller.reconcile_once(
        paths,
        node="node-158",
        views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True,
    )
    assert out["alerted"] == ["skgateway"] and out["failovers"] == []
    assert store.read_placement(paths, "service", "skgateway")["node"] == "node-41"
    assert alerted and "node-41" in alerted[0] and "skgateway" in alerted[0]
    # second pass inside the dedupe window: event suppressed, alert suppressed
    alerted.clear()
    service_controller.reconcile_once(
        paths,
        node="node-158",
        views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True,
    )
    assert alerted == []


def test_auto_failover_replaces_onto_live_node(paths, operator) -> None:
    _svc(paths, operator, failover="auto")
    service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    out = service_controller.reconcile_once(
        paths, node="node-158", views=_views(dead41=True), alert=lambda *a, **k: True
    )
    assert out["failovers"] == ["skgateway"]
    placement = store.read_placement(paths, "service", "skgateway")
    assert placement["node"] == "node-158"  # Dead node filtered out
    assert placement["placementGeneration"] == 2


def test_frozen_blocks_placements_but_not_the_dead_alert(paths, operator) -> None:
    _svc(paths, operator)
    service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    store.set_frozen(paths, True, writer=operator, reason="drill")
    _svc(paths, operator, name="skcomms")  # new, unplaced service
    alerted: list[str] = []
    out = service_controller.reconcile_once(
        paths,
        node="node-158",
        views=_views(dead41=True),
        alert=lambda msg, **kw: alerted.append(msg) or True,
    )
    assert store.read_placement(paths, "service", "skcomms") is None  # frozen
    assert out["alerted"] == ["skgateway"] and alerted  # alert lives


def test_deleted_and_invalid_specs_are_skipped(paths, operator) -> None:
    _svc(paths, operator, deleted=True)
    store.write_spec(
        paths, "service", "broken", {"runtime": "docker"}, writer=operator
    )  # no unit: invalid
    out = service_controller.reconcile_once(
        paths, node="node-158", views=_views(), alert=lambda *a, **k: True
    )
    assert out["placed"] == []
    assert sorted(out["skipped"]) == ["broken", "skgateway"]


def test_service_rows_drift_and_unknown(paths, operator, scheduler_writer, noded41) -> None:
    _svc(paths, operator)
    store.write_placement(
        paths, "service", "skgateway", node="node-41", reason="r", writer=scheduler_writer
    )
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].ready == "Unknown"  # no observation yet
    assert rows["skgateway"].state == "unobserved"
    store.write_status(
        paths,
        "service",
        "skgateway",
        node="node-41",
        status={
            "state": "active",
            "pid": 1,
            "since": "t",
            "restarts": 0,
            "runtime": "systemd-user",
        },
        conditions=[
            {
                "type": "Ready",
                "status": "True",
                "reason": "UnitActive",
                "message": "ok",
                "lastTransition": "t",
            }
        ],
        observed_generation=1,
        writer=noded41,
    )
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].ready == "True" and rows["skgateway"].state == "active"
    assert rows["skgateway"].stale is False
    store.write_spec(
        paths,
        "service",
        "skgateway",
        {"unit": "skgateway.service", "paused": True},
        writer=operator,
    )
    rows = {r.name: r for r in service_controller.service_rows(paths)}
    assert rows["skgateway"].stale is True
    assert rows["skgateway"].ready == "Unknown"  # stale renders Unknown
    assert rows["skgateway"].paused is True
