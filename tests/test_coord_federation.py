"""Multi-host coordination projection and conflict safety tests."""

import asyncio
import json
from pathlib import Path

from skcapstone.coord_federation import (
    CoordFederationWatcher,
    host_scoped_agent_dir,
    projection_path,
)


def test_hosts_write_distinct_projection_paths(tmp_path: Path):
    coordination = tmp_path / "coordination"
    first = projection_path(coordination, "pi-worker", "chiap01")
    second = projection_path(coordination, "pi-worker", "chiap02")
    assert first != second
    assert first.parent == host_scoped_agent_dir(coordination, "chiap01")
    assert second.parent == host_scoped_agent_dir(coordination, "chiap02")


def test_projection_path_sanitizes_host_and_agent_names(tmp_path: Path):
    path = projection_path(tmp_path, "../worker name", "host/name")
    assert path == tmp_path / "agents" / "host_name" / "worker_name.json"


def test_conflict_is_preserved_and_announced_for_reconciliation(tmp_path: Path):
    coordination = tmp_path / "coordination"
    conflict = (
        coordination / "agents" / "chiap01" / "worker.sync-conflict-20260906-120000-ABC123.json"
    )
    conflict.parent.mkdir(parents=True)
    original = {"agent": "worker", "state": "active", "claim": "556491d9"}
    conflict.write_text(json.dumps(original), encoding="utf-8")

    watcher = CoordFederationWatcher(tmp_path)
    events = []

    async def announce(path, event="synced"):
        events.append((path, event))

    watcher._announce = announce
    asyncio.run(watcher._handle_change(conflict))

    assert conflict.exists()
    assert json.loads(conflict.read_text(encoding="utf-8")) == original
    assert events == [(conflict, "conflict_reconciliation_required")]


def test_malformed_projection_is_ignored_without_mutation(tmp_path: Path):
    coordination = tmp_path / "coordination"
    malformed = coordination / "agents" / "chiap01" / "worker.json"
    malformed.parent.mkdir(parents=True)
    malformed.write_text('{"claim":', encoding="utf-8")

    watcher = CoordFederationWatcher(tmp_path)
    events = []

    async def announce(path, event="synced"):
        events.append((path, event))

    watcher._announce = announce
    asyncio.run(watcher._handle_change(malformed))

    assert malformed.read_text(encoding="utf-8") == '{"claim":'
    assert events == []
