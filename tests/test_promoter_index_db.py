"""Regression: the memory promoter must keep skmemory's SQLite index.db in sync.

Incident (2026-07-12 "skmemory drift"): archive_old_memories/_archive_deduped
moved flat files into memory/archive/ and called _remove_from_index, but that
only edited index.json — never index.db. Stale index.db rows then showed up as
phantom orphans in `skmemory health`, producing permanent false-DRIFT reports.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.memory_engine import _remove_from_index
from skcapstone.memory_promoter import PromotionEngine
from skcapstone.models import MemoryEntry, MemoryLayer


@pytest.fixture
def home(tmp_path: Path) -> Path:
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    for layer in ("short-term", "mid-term", "long-term"):
        (mem_dir / layer).mkdir()
    return tmp_path


def _make_index_db(home: Path, ids: list[str]) -> Path:
    """Create a minimal skmemory-shaped index.db seeded with the given ids."""
    db = home / "memory" / "index.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, layer TEXT, file_path TEXT)")
    for mid in ids:
        conn.execute(
            "INSERT INTO memories (id, layer, file_path) VALUES (?, ?, ?)",
            (mid, "short-term", str(home / "memory" / "short-term" / f"{mid}.json")),
        )
    conn.commit()
    conn.close()
    return db


def _row_exists(db: Path, mid: str) -> bool:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT 1 FROM memories WHERE id = ?", (mid,)).fetchone() is not None
    finally:
        conn.close()


def _write_memory(home: Path, mid: str, age_days: int) -> MemoryEntry:
    entry = MemoryEntry(
        memory_id=mid,
        content=f"content for {mid}",
        tags=[],
        source="test",
        layer=MemoryLayer.SHORT_TERM,
        importance=0.5,
        created_at=datetime.now(timezone.utc) - timedelta(days=age_days),
    )
    path = home / "memory" / "short-term" / f"{mid}.json"
    path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
    return entry


def test_remove_from_index_deletes_sqlite_row(home: Path) -> None:
    db = _make_index_db(home, ["keep", "drop"])
    _remove_from_index(home, "drop")
    assert not _row_exists(db, "drop")
    assert _row_exists(db, "keep")


def test_remove_from_index_no_db_is_noop(home: Path) -> None:
    # No index.db present — must not raise.
    _remove_from_index(home, "whatever")


def test_archive_old_memories_prunes_sqlite_index(home: Path) -> None:
    old = _write_memory(home, "old-mem", age_days=90)
    _write_memory(home, "young-mem", age_days=1)
    db = _make_index_db(home, ["old-mem", "young-mem"])

    engine = PromotionEngine(home)
    archived = engine.archive_old_memories()

    assert archived == 1
    assert (home / "memory" / "archive" / "old-mem.json").exists()
    assert not _row_exists(db, "old-mem"), "archived memory left a stale index.db row"
    assert _row_exists(db, "young-mem")
