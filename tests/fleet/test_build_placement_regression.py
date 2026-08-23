"""Build work must target .41, never the control box (card 945fd6d3).

The live fleet carried `heavy-build=true` on BOTH node-noroc2027 (the control
node, 4 cores and under 4G of free disk) and node-41 (20 cores, 739G). Since
`feasible()` filters on cores and ram only and never looks at disk, a build
with modest declared requests but a large real disk footprint could be placed
on the tiny control box and fill its root filesystem.

The label was removed from the live control node. These tests pin the
consequence using the REAL measured capacity numbers, so the drift cannot
quietly come back.
"""

from __future__ import annotations

import pytest

from skcapstone.fleet import scheduler
from skcapstone.fleet.node_controller import NodeView
from skcapstone.fleet.scheduler import Workload

# Measured 2026-08-15 from the live status reports, not invented.
CONTROL_CAPACITY = {"cores": 4, "ram_gb": 5.6, "disk_gb": 4.0}
BUILDER_CAPACITY = {"cores": 20, "ram_gb": 30.2, "disk_gb": 739.5}


def _control(*, heavy_build: bool) -> NodeView:
    labels = {"always-on": "true", "control-plane": "true", "dev-primary": "true"}
    if heavy_build:
        labels["heavy-build"] = "true"
    return NodeView(
        name="node-noroc2027",
        phase="Ready",
        labels=labels,
        capacity=dict(CONTROL_CAPACITY),
        allocatable=dict(CONTROL_CAPACITY),
        heartbeat_age_s=5.0,
        role="control",
    )


def _builder() -> NodeView:
    return NodeView(
        name="node-41",
        phase="Ready",
        labels={"heavy-build": "true"},
        capacity=dict(BUILDER_CAPACITY),
        allocatable=dict(BUILDER_CAPACITY),
        heartbeat_age_s=5.0,
        role="builder-standby",
    )


def _build_workload() -> Workload:
    """A build that declares modest requests, which is the dangerous shape.

    Requests are advisory and cover cores and ram only. The real cost of a
    build is disk, which nothing checks, so a build that looks small sails
    through the filter and then fills the box.
    """
    return Workload(
        kind="job",
        name="heavy-build-job",
        node_selector={"heavy-build": "true"},
        tolerations=(),
        requests={"cores": 2, "ram_gb": 2.0},
    )


def test_build_lands_on_the_builder_not_the_control_box() -> None:
    decision = scheduler.select([_control(heavy_build=False), _builder()], _build_workload())
    assert decision.node == "node-41"


def test_the_control_node_is_excluded_by_selector_once_the_label_is_gone() -> None:
    reason = scheduler.feasible(_control(heavy_build=False), _build_workload())
    assert reason is not None
    assert "selector" in reason


def test_the_label_was_the_only_thing_standing_between_a_build_and_the_control_box() -> None:
    """Negative control: with heavy-build still on the control node, it IS a
    feasible target despite having 4.0G of disk. This is what was true in the
    live store until 2026-08-15, and it is why the label was removed."""
    assert scheduler.feasible(_control(heavy_build=True), _build_workload()) is None


def test_feasible_still_does_not_check_disk() -> None:
    """Pins the underlying gap rather than pretending the label fix closed it.

    Removing the label stops THIS build from reaching THAT box. It does not
    teach the scheduler about disk. A workload declaring a disk request is
    still admitted to a node with far less disk than it asks for, so a future
    card should either honor disk in feasible() or stop accepting the key.
    """
    starved = NodeView(
        name="node-tiny",
        phase="Ready",
        labels={"heavy-build": "true"},
        capacity={"cores": 8, "ram_gb": 16.0, "disk_gb": 1.0},
        allocatable={"cores": 8, "ram_gb": 16.0, "disk_gb": 1.0},
        heartbeat_age_s=5.0,
    )
    hungry = Workload(
        kind="job",
        name="disk-hungry",
        node_selector={"heavy-build": "true"},
        tolerations=(),
        requests={"cores": 1, "ram_gb": 1.0, "disk_gb": 500.0},
    )
    assert scheduler.feasible(starved, hungry) is None


@pytest.mark.parametrize("cores,ram", [(64, 128.0), (2, 1.0)])
def test_selector_beats_capacity_in_both_directions(cores: int, ram: float) -> None:
    """A node without the label is never chosen no matter how big it is, and
    the labelled node is chosen even when it is the only candidate."""
    unlabelled = NodeView(
        name="node-huge",
        phase="Ready",
        labels={},
        capacity={"cores": cores, "ram_gb": ram, "disk_gb": 9000.0},
        allocatable={"cores": cores, "ram_gb": ram, "disk_gb": 9000.0},
        heartbeat_age_s=5.0,
    )
    decision = scheduler.select([unlabelled, _builder()], _build_workload())
    assert decision.node == "node-41"
