"""Table-driven placement decisions (Card 2.1 acceptance, pinned table)."""

from __future__ import annotations

import pytest

from skcapstone.fleet import events, scheduler, store
from skcapstone.fleet.node_controller import NodeView


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _views() -> list[NodeView]:
    return [
        NodeView(
            name="node-158",
            phase="Ready",
            labels={"always-on": "true", "control-plane": "true"},
            allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0},
        ),
        NodeView(
            name="node-41",
            phase="Ready",
            labels={"heavy-build": "true"},
            allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0},
        ),
        NodeView(
            name="node-100",
            phase="Ready",
            labels={"gpu": "true"},
            taints=[{"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}],
            allocatable={"cores": 11, "ram_gb": 20.0, "disk_gb": 300.0},
        ),
        NodeView(
            name="node-local",
            phase="Ready",
            labels={"interactive": "true"},
            taints=[{"key": "interactive", "value": "true", "effect": "PreferNoSchedule"}],
            allocatable={"cores": 3, "ram_gb": 6.0, "disk_gb": 40.0},
        ),
    ]


# The PINNED table (Card 2.3 acceptance: shown reasons must match this).
TABLE = [
    ({}, "node-41", "least-loaded: node-41"),
    (
        {"node_selector": {"gpu": "true"}, "tolerations": ({"key": "dedicated"},)},
        "node-100",
        "least-loaded: node-100",
    ),
    ({"node_selector": {"heavy-build": "true"}}, "node-41", "least-loaded: node-41"),
    ({"node_selector": {"gpu": "true"}}, None, "unschedulable"),
]


@pytest.mark.parametrize("kw,expected,fragment", TABLE)
def test_decision_table(kw, expected, fragment) -> None:
    decision = scheduler.select(_views(), scheduler.Workload(kind="job", name="c", **kw))
    assert decision.node == expected
    assert fragment in decision.reason


def test_least_loaded_and_deterministic_tiebreak() -> None:
    a = NodeView(
        name="node-b", phase="Ready", allocatable={"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0}
    )
    b = NodeView(
        name="node-a", phase="Ready", allocatable={"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0}
    )
    wl = scheduler.Workload(kind="job", name="c")
    assert scheduler.select([a, b], wl).node == "node-a"  # lexicographic tiebreak
    assert scheduler.select([b, a], wl).node == "node-a"  # input order irrelevant
    bigger = NodeView(
        name="node-z", phase="Ready", allocatable={"cores": 4, "ram_gb": 9.0, "disk_gb": 50.0}
    )
    assert scheduler.select([a, b, bigger], wl).node == "node-z"


def test_cordon_excludes_with_recorded_reason() -> None:
    views = _views()
    views[1] = NodeView(
        name="node-41",
        phase="Ready",
        cordoned=True,
        labels={"heavy-build": "true"},
        allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0},
    )
    decision = scheduler.select(views, scheduler.Workload(kind="job", name="c"))
    assert decision.node == "node-158"  # next-most headroom survivor
    assert decision.excluded["node-41"] == "cordoned"


def test_advisory_prefer_noschedule_recorded_in_reason() -> None:
    views = [
        NodeView(
            name="node-local",
            phase="Ready",
            taints=[{"key": "interactive", "value": "true", "effect": "PreferNoSchedule"}],
            allocatable={"cores": 3, "ram_gb": 6.0, "disk_gb": 40.0},
        )
    ]
    decision = scheduler.select(views, scheduler.Workload(kind="job", name="c"))
    assert decision.node == "node-local"
    assert "advisory: PreferNoSchedule taint interactive=true" in decision.reason


def test_place_writes_once_and_honors_freeze(paths, operator) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    wl = scheduler.Workload(kind="job", name="card-1")
    placement = scheduler.place(paths, wl, writer=sched, views=_views())
    assert placement["node"] == "node-41"
    assert placement["placementGeneration"] == 1
    again = scheduler.place(paths, wl, writer=sched, views=_views())
    assert again["placementGeneration"] == 1  # idempotent re-run: no churn
    store.set_frozen(paths, True, writer=operator, reason="drill")
    assert (
        scheduler.place(
            paths, scheduler.Workload(kind="job", name="card-2"), writer=sched, views=_views()
        )
        is None
    )
    assert store.read_placement(paths, "job", "card-2") is None  # frozen: no writes


def test_place_unschedulable_writes_nothing(paths) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    wl = scheduler.Workload(kind="job", name="card-3", node_selector={"nonexistent": "true"})
    assert scheduler.place(paths, wl, writer=sched, views=_views()) is None
    assert store.read_placement(paths, "job", "card-3") is None
