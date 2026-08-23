"""Tests for skfleet placements (Card 2.3): visible decisions with reasons."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import events, scheduler, store
from skcapstone.fleet.cli import fleet
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
            allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0},
        ),
        NodeView(
            name="node-41",
            phase="Ready",
            labels={"heavy-build": "true"},
            allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0},
        ),
    ]


def _place_two(paths) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    scheduler.place(
        paths, scheduler.Workload(kind="job", name="card-1"), writer=sched, views=_views()
    )
    scheduler.place(
        paths,
        scheduler.Workload(kind="job", name="card-2", node_selector={"heavy-build": "true"}),
        writer=sched,
        views=_views(),
    )


def test_placements_listing_shows_node_and_reason(paths) -> None:
    runner = CliRunner()
    env = {"SKFLEET_ROOT": str(paths.root)}
    assert "no placements" in runner.invoke(fleet, ["placements"], env=env).output
    _place_two(paths)
    out = runner.invoke(fleet, ["placements"], env=env)
    assert out.exit_code == 0
    assert "job/card-1" in out.output and "-> node-41" in out.output
    assert "least-loaded: node-41" in out.output  # the reason column
    payload = json.loads(runner.invoke(fleet, ["placements", "--json"], env=env).output)
    assert [p["name"] for p in payload] == ["card-1", "card-2"]
    # reasons match the pinned scheduler test table (Task 4 TABLE rows 1 and 3)
    assert all(p["reason"].startswith("least-loaded: node-41") for p in payload)
    only_jobs = json.loads(
        runner.invoke(fleet, ["placements", "--kind", "job", "--json"], env=env).output
    )
    assert len(only_jobs) == 2


def test_every_placement_decision_is_logged(paths) -> None:
    _place_two(paths)
    for name in ("card-1", "card-2"):
        logged = events.read(paths, "node-158", kind="job", name=name)
        assert logged, f"no Placement event for {name}"
        assert logged[-1]["type"] == "Placement"
        assert logged[-1]["reason"] == "Placed"
        assert logged[-1]["message"].startswith("least-loaded: node-41")
