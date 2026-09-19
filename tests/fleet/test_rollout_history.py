"""Tests for the rollout history (spec A.6, staged-rollout Task 1).

Rollback needs a previous state, and nothing recorded one before this: the
only rollback that exists in code today (``lifecycle_seats``) restores its
own config files, and the wheel-level rollback convention used in practice
is hand-written evidence in ``card_events`` with no backing code. These
tests hold ``record_deployment``/``previous_manifest`` to the properties
that make "the previous manifest" a fact instead of a reconstruction: the
history is append-only (an earlier record is never rewritten or dropped by
a later write), a single corrupt entry costs only that entry (never the
whole file, and never silently -- it must show up as a visible warning),
and the store lives under a node-scoped path so five Syncthing-shared hosts
never overwrite each other's history.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from skcapstone.fleet import rollout_history
from skcapstone.fleet.paths import self_node_name
from skcapstone.fleet.rollout_history import previous_manifest, record_deployment


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway estate home, never the real ~/.skcapstone."""
    return tmp_path / "home"


def _history_file(home: Path) -> Path:
    """The concrete on-disk path this module is expected to use, built the
    same way ``rollout_history._history_path`` builds it, so a test can
    assert on the real file without importing the private helper for every
    check.
    """
    return (
        home / ".skcapstone" / "fleet" / "status" / self_node_name() / "rollout" / "history.jsonl"
    )


def _manifest(revision: str) -> dict:
    return {
        "revision": revision,
        "git_sha": "deadbeef",
        "package_version": "0.1.0",
        "required_env": ["SKFLEET_TARGET"],
        "units": ["skcapstone.service"],
    }


def test_first_record_has_no_previous(home: Path) -> None:
    record_deployment(home, _manifest("rev-1"))
    assert previous_manifest(home) is None


def test_recording_twice_makes_the_first_the_previous(home: Path) -> None:
    record_deployment(home, _manifest("rev-1"))
    record_deployment(home, _manifest("rev-2"))
    assert previous_manifest(home) == _manifest("rev-1")


def test_recording_a_third_time_shifts_previous_forward(home: Path) -> None:
    record_deployment(home, _manifest("rev-1"))
    record_deployment(home, _manifest("rev-2"))
    record_deployment(home, _manifest("rev-3"))
    assert previous_manifest(home) == _manifest("rev-2")


def test_store_is_append_only_earlier_entry_survives_a_later_write(home: Path) -> None:
    record_deployment(home, _manifest("rev-1"))
    path = _history_file(home)
    first_write_bytes = path.read_bytes()

    record_deployment(home, _manifest("rev-2"))
    second_write_bytes = path.read_bytes()

    # The exact bytes written for rev-1 are still present, byte for byte,
    # as a prefix of the file after a later write -- not just "the same
    # logical entry re-derived", but the literal earlier write undisturbed.
    assert second_write_bytes.startswith(first_write_bytes)
    assert second_write_bytes != first_write_bytes

    lines = [line for line in second_write_bytes.decode("utf-8").splitlines() if line]
    assert len(lines) == 2
    assert json.loads(lines[0]) == _manifest("rev-1")
    assert json.loads(lines[1]) == _manifest("rev-2")


def test_corrupt_entry_is_skipped_visibly_and_the_rest_still_reads(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record_deployment(home, _manifest("rev-1"))
    path = _history_file(home)
    # Corrupt the middle of the history by hand, simulating a partial write
    # or bit rot a prior process left behind: this must cost that one
    # entry, not the file.
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{not valid json at all\n")
    record_deployment(home, _manifest("rev-2"))

    with caplog.at_level(logging.WARNING):
        result = previous_manifest(home)

    # Only rev-1 and rev-2 are readable entries (the corrupt line between
    # them is skipped, not counted), so "previous" -- the second-to-last
    # READABLE entry -- is rev-1: the corrupt line cost only itself, not
    # the reading of rev-1 or rev-2 around it.
    assert result == _manifest("rev-1")
    assert any("rollout_history" in record.name for record in caplog.records)
    assert any(
        "corrupt" in record.message.lower() or "skip" in record.message.lower()
        for record in caplog.records
    )

    # And the corrupt line itself is still sitting in the file, unmodified,
    # proving the guard did not silently drop it to make the file "clean" --
    # a discoverable trace survives for an operator to go look at.
    raw = path.read_text(encoding="utf-8")
    assert "not valid json" in raw


def test_corrupt_entry_does_not_break_reading_entries_around_it(
    home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    record_deployment(home, _manifest("rev-1"))
    record_deployment(home, _manifest("rev-2"))
    path = _history_file(home)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("{not valid json at all\n")
    record_deployment(home, _manifest("rev-3"))

    with caplog.at_level(logging.WARNING):
        result = previous_manifest(home)

    # rev-2 is the last entry readable before rev-3, skipping straight over
    # the corrupt line in between.
    assert result == _manifest("rev-2")


def test_path_is_node_scoped(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKFLEET_NODE", "node-alpha")
    record_deployment(home, _manifest("rev-1"))

    expected = (
        home / ".skcapstone" / "fleet" / "status" / "node-alpha" / "rollout" / "history.jsonl"
    )
    assert expected.exists()

    # A different node under the same home gets an isolated history: two
    # hosts sharing one Syncthing-synced ~/.skcapstone must never write
    # into the same file.
    monkeypatch.setenv("SKFLEET_NODE", "node-beta")
    record_deployment(home, _manifest("rev-1"))
    other = home / ".skcapstone" / "fleet" / "status" / "node-beta" / "rollout" / "history.jsonl"
    assert other.exists()
    assert previous_manifest(home) is None  # node-beta's own first record


def test_history_path_helper_matches_node_scoped_convention(home: Path) -> None:
    """rollout_history builds its path from self_node_name(), the same
    function paths.py and rollout_drift.py already use, rather than a
    second, independent notion of "this node".
    """
    path = rollout_history._history_path(home)
    assert path == _history_file(home)
