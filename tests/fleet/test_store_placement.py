"""Tests for placement writes: scheduler ownership, generation, write-on-change."""

from __future__ import annotations

import pytest

from skcapstone.fleet import events, store


def test_write_placement_bumps_generation_on_change(paths, scheduler_writer) -> None:
    first, changed = store.write_placement(
        paths, "job", "card-1", node="node-158", reason="least-loaded", writer=scheduler_writer
    )
    assert changed is True
    assert first["placementGeneration"] == 1
    assert first["node"] == "node-158"
    assert first["kind"] == "Job"
    moved, changed = store.write_placement(
        paths, "job", "card-1", node="node-41", reason="least-loaded", writer=scheduler_writer
    )
    assert changed is True and moved["placementGeneration"] == 2
    assert store.read_placement(paths, "job", "card-1")["node"] == "node-41"


def test_write_placement_idempotent(paths, scheduler_writer) -> None:
    store.write_placement(
        paths, "job", "card-1", node="node-158", reason="r", writer=scheduler_writer
    )
    again, changed = store.write_placement(
        paths, "job", "card-1", node="node-158", reason="r", writer=scheduler_writer
    )
    assert changed is False
    assert again["placementGeneration"] == 1  # unchanged input: zero churn


def test_only_scheduler_writes_placements(paths, operator, noded41) -> None:
    for writer in (operator, noded41):
        with pytest.raises(store.OwnershipError):
            store.write_placement(
                paths, "job", "card-1", node="node-158", reason="r", writer=writer
            )


def test_bad_names_rejected(paths, scheduler_writer) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_placement(
            paths, "job", "../evil", node="node-158", reason="r", writer=scheduler_writer
        )


def test_list_placements_sorted_and_filtered(paths, scheduler_writer) -> None:
    assert store.list_placements(paths) == []
    store.write_placement(
        paths, "job", "card-b", node="node-158", reason="r", writer=scheduler_writer
    )
    store.write_placement(
        paths, "job", "card-a", node="node-41", reason="r", writer=scheduler_writer
    )
    store.write_placement(
        paths, "service", "skgateway", node="node-158", reason="r", writer=scheduler_writer
    )
    assert [(p["kind"], p["name"]) for p in store.list_placements(paths)] == [
        ("Job", "card-a"),
        ("Job", "card-b"),
        ("Service", "skgateway"),
    ]
    assert [p["name"] for p in store.list_placements(paths, "job")] == ["card-a", "card-b"]


def test_merged_includes_placement(paths, operator, scheduler_writer) -> None:
    store.write_spec(paths, "service", "skgateway", {"unit": "u"}, writer=operator)
    store.write_placement(
        paths, "service", "skgateway", node="node-158", reason="r", writer=scheduler_writer
    )
    assert store.merged(paths, "service", "skgateway")["placement"]["node"] == "node-158"


def test_scheduler_may_emit_events(paths, scheduler_writer, operator) -> None:
    events.reset_dedupe()
    assert (
        events.emit(
            paths,
            scheduler_writer,
            kind="job",
            name="card-1",
            type="Placement",
            reason="Placed",
            message="m",
            now=1000.0,
        )
        is True
    )
    with pytest.raises(store.OwnershipError):
        events.emit(
            paths,
            operator,
            kind="job",
            name="card-1",
            type="Placement",
            reason="Placed",
            message="m",
            now=1001.0,
        )
    events.reset_dedupe()
