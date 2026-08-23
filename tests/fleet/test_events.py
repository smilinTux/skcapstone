"""Tests for the bounded per-node event log."""

from __future__ import annotations

import pytest

from skcapstone.fleet import events, store


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _emit(paths, noded41, *, reason="Started", now=1000.0) -> bool:
    return events.emit(
        paths,
        noded41,
        kind="service",
        name="skgateway",
        type="Actuation",
        reason=reason,
        message="m",
        now=now,
    )


def test_emit_appends_and_read_filters(paths, noded41) -> None:
    assert _emit(paths, noded41) is True
    assert _emit(paths, noded41, reason="Stopped", now=1001.0) is True
    all_events = events.read(paths, "node-41")
    assert [e["reason"] for e in all_events] == ["Started", "Stopped"]
    assert events.read(paths, "node-41", kind="service", name="other") == []
    assert all_events[0]["node"] == "node-41"


def test_dedupe_window(paths, noded41) -> None:
    assert _emit(paths, noded41, now=1000.0) is True
    assert _emit(paths, noded41, now=1100.0) is False  # inside window
    assert _emit(paths, noded41, now=1000.0 + 301.0) is True  # window passed


def test_rotation_bounded_to_two_files(paths, noded41, monkeypatch) -> None:
    monkeypatch.setattr(events, "MAX_BYTES", 200)
    for i in range(20):
        assert _emit(paths, noded41, reason=f"r{i}", now=1000.0 + i) is True
    live = paths.events_path("node-41")
    rotated = live.with_name("events.jsonl.1")
    assert live.exists() and rotated.exists()
    assert live.stat().st_size <= 400
    siblings = [p.name for p in live.parent.iterdir() if p.name.startswith("events.jsonl")]
    assert sorted(siblings) == ["events.jsonl", "events.jsonl.1"]  # two files, ever
    assert events.read(paths, "node-41", limit=5)[-1]["reason"] == "r19"


def test_emit_ownership(paths, operator) -> None:
    with pytest.raises(store.OwnershipError):
        events.emit(
            paths, operator, kind="service", name="x", type="Actuation", reason="r", message="m"
        )
