"""Regression tests for fail-closed coordination conflict handling."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from skcapstone.coord_federation import CoordFederationWatcher


def _replica_conflicts(
    tmp_path: Path,
    *,
    canonical: bytes | None,
    conflict: bytes,
    conflict_is_newer: bool = False,
) -> list[tuple[CoordFederationWatcher, Path, Path]]:
    """Create the same conflict scenario on two independent replicas."""
    replicas = []
    for name in ("replica-a", "replica-b"):
        root = tmp_path / name
        tasks = root / "coordination" / "tasks"
        tasks.mkdir(parents=True)
        canonical_path = tasks / "card.json"
        conflict_path = tasks / "card.sync-conflict-20260916-010203-PEER1.json"
        if canonical is not None:
            canonical_path.write_bytes(canonical)
        conflict_path.write_bytes(conflict)
        if canonical is not None:
            older, newer = (
                (canonical_path, conflict_path)
                if conflict_is_newer
                else (
                    conflict_path,
                    canonical_path,
                )
            )
            os.utime(older, (1, 1))
            os.utime(newer, (2, 2))
        replicas.append(
            (CoordFederationWatcher(root, agent_name=name), canonical_path, conflict_path)
        )
    return replicas


@pytest.mark.asyncio
async def test_absent_canonical_conflict_is_preserved_on_both_replicas(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A conflict must never be promoted when its canonical file is absent."""
    conflict_bytes = b'{"replica":"peer"}\n'
    replicas = _replica_conflicts(tmp_path, canonical=None, conflict=conflict_bytes)

    with caplog.at_level(logging.WARNING, logger="skcapstone.coord_federation"):
        for watcher, canonical_path, conflict_path in replicas:
            await watcher._handle_change(conflict_path)
            assert not canonical_path.exists()
            assert conflict_path.read_bytes() == conflict_bytes

    digest = hashlib.sha256(conflict_bytes).hexdigest()
    assert sum(digest in record.message for record in caplog.records) == 2
    assert sum("canonical_sha256=absent" in record.message for record in caplog.records) == 2


@pytest.mark.asyncio
async def test_newer_conflict_never_replaces_canonical_on_both_replicas(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A newer conflict mtime must not select or replace either byte stream."""
    canonical_bytes = b'{"winner":"canonical"}\n'
    conflict_bytes = b'{"newer":"conflict"}\n'
    replicas = _replica_conflicts(
        tmp_path,
        canonical=canonical_bytes,
        conflict=conflict_bytes,
        conflict_is_newer=True,
    )

    with caplog.at_level(logging.WARNING, logger="skcapstone.coord_federation"):
        for watcher, canonical_path, conflict_path in replicas:
            await watcher._handle_change(conflict_path)
            assert canonical_path.read_bytes() == canonical_bytes
            assert conflict_path.read_bytes() == conflict_bytes

    canonical_digest = hashlib.sha256(canonical_bytes).hexdigest()
    conflict_digest = hashlib.sha256(conflict_bytes).hexdigest()
    assert sum(canonical_digest in record.message for record in caplog.records) == 2
    assert sum(conflict_digest in record.message for record in caplog.records) == 2


@pytest.mark.asyncio
async def test_older_conflict_loser_is_not_deleted_on_both_replicas(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An older conflict must remain intact rather than being treated as a loser."""
    canonical_bytes = b'{"newer":"canonical"}\n'
    conflict_bytes = b'{"loser":"must-survive"}\n'
    replicas = _replica_conflicts(
        tmp_path,
        canonical=canonical_bytes,
        conflict=conflict_bytes,
    )

    with caplog.at_level(logging.WARNING, logger="skcapstone.coord_federation"):
        for watcher, canonical_path, conflict_path in replicas:
            await watcher._handle_change(conflict_path)
            assert canonical_path.read_bytes() == canonical_bytes
            assert conflict_path.read_bytes() == conflict_bytes

    preserved = [
        record
        for record in caplog.records
        if "coordination conflict preserved" in record.message.lower()
    ]
    assert len(preserved) == 2


@pytest.mark.asyncio
async def test_non_conflict_announcement_is_unchanged(tmp_path: Path) -> None:
    """A normal coordination JSON change still emits the synced announcement."""
    root = tmp_path / "replica"
    path = root / "coordination" / "tasks" / "card.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"id":"card"}\n')
    watcher = CoordFederationWatcher(root)
    watcher._announce = AsyncMock()

    await watcher._handle_change(path)

    watcher._announce.assert_awaited_once_with(path, event="synced")


@pytest.mark.asyncio
async def test_conflict_outside_coordination_root_is_ignored(tmp_path: Path) -> None:
    """A direct callback cannot process a conflict beyond the watched root."""
    root = tmp_path / "replica"
    path = tmp_path / "outside.sync-conflict-20260916-010203-PEER1.json"
    original = b'{"outside":true}\n'
    path.write_bytes(original)
    watcher = CoordFederationWatcher(root)

    await watcher._handle_change(path)

    assert path.read_bytes() == original
    assert not (tmp_path / "outside.json").exists()


@pytest.mark.asyncio
async def test_in_root_conflict_never_reads_external_canonical_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An in-root conflict must not hash a canonical symlink outside the root."""
    root = tmp_path / "replica"
    tasks = root / "coordination" / "tasks"
    tasks.mkdir(parents=True)
    external = tmp_path / "external.json"
    external.write_bytes(b'{"external":"must-not-be-read"}\n')
    canonical = tasks / "card.json"
    canonical.symlink_to(external)
    conflict = tasks / "card.sync-conflict-20260916-010203-PEER1.json"
    conflict_bytes = b'{"conflict":"preserve"}\n'
    conflict.write_bytes(conflict_bytes)
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path: Path) -> bytes:
        """Fail if the watcher tries to read the external canonical target."""
        if path.resolve() == external:
            raise AssertionError("external canonical target was read")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    with caplog.at_level(logging.WARNING, logger="skcapstone.coord_federation"):
        await CoordFederationWatcher(root)._handle_change(conflict)

    assert conflict.read_bytes() == conflict_bytes
    assert canonical.is_symlink()
    assert original_read_bytes(external) == b'{"external":"must-not-be-read"}\n'
    assert any("canonical_sha256=unsafe" in record.message for record in caplog.records)
