"""Table-driven tests for scheduler soft preference scoring (Card 2.1b).

PreferNoSchedule soft-avoid layered on top of the v1 filter and
least-loaded tiebreak (fleet Phase 2 plan, Task 2.1b). feasible() stays
untouched; only select()'s ranking gains a preference pass ahead of the
existing headroom tiebreak.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import scheduler
from skcapstone.fleet.node_controller import NodeView

PREFER_TAINT = [{"key": "interactive", "value": "true", "effect": "PreferNoSchedule"}]


def _view(name, ram, cores=4, taints=None) -> NodeView:
    return NodeView(
        name=name,
        phase="Ready",
        taints=taints or [],
        allocatable={"cores": cores, "ram_gb": ram, "disk_gb": 50.0},
    )


# The PINNED preference table (Card 2.1b acceptance).
TABLE = [
    # Untainted node wins even with slightly less headroom than a tainted one.
    (
        [_view("node-plain", ram=5.9), _view("node-local", ram=6.0, taints=PREFER_TAINT)],
        "node-plain",
        "least-loaded: node-plain",
    ),
    # A tainted node is chosen only when it is the sole feasible candidate.
    (
        [_view("node-local", ram=6.0, taints=PREFER_TAINT)],
        "node-local",
        "least-loaded: node-local",
    ),
    # No PreferNoSchedule interaction anywhere: identical to v1 least-loaded.
    (
        [_view("node-a", ram=8.0), _view("node-b", ram=9.0)],
        "node-b",
        "least-loaded: node-b",
    ),
]


@pytest.mark.parametrize("views,expected,fragment", TABLE)
def test_preference_decision_table(views, expected, fragment) -> None:
    decision = scheduler.select(views, scheduler.Workload(kind="job", name="c"))
    assert decision.node == expected
    assert fragment in decision.reason


def test_prefer_no_schedule_deprioritized_reason_recorded() -> None:
    tainted = _view("node-local", ram=20.0, taints=PREFER_TAINT)
    plain = _view("node-plain", ram=6.0)
    decision = scheduler.select([tainted, plain], scheduler.Workload(kind="job", name="c"))
    assert decision.node == "node-plain"
    assert "soft-avoid" in decision.reason
    assert "interactive=true" in decision.reason


def test_tolerated_prefer_no_schedule_is_not_deprioritized() -> None:
    tainted = _view("node-local", ram=20.0, taints=PREFER_TAINT)
    plain = _view("node-plain", ram=6.0)
    wl = scheduler.Workload(kind="job", name="c", tolerations=({"key": "interactive"},))
    decision = scheduler.select([tainted, plain], wl)
    assert decision.node == "node-local"  # tolerated: no soft-avoid, headroom wins


def test_no_regression_when_no_prefer_no_schedule_taints() -> None:
    a = _view("node-a", ram=8.0)
    b = _view("node-b", ram=8.0)
    wl = scheduler.Workload(kind="job", name="c")
    assert scheduler.select([a, b], wl).node == "node-a"  # same tiebreak as v1
