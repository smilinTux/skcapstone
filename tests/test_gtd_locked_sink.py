"""gtd_tools writes go through the shared locked / atomic / deduped sink.

Regression tests for card 272845a7: the MCP GTD path was the last writer
bypassing the locked skos.gtd_ingest sink (bare path.write_text, no flock, no
tmp+fsync+os.replace atomicity, no whole-store (source, source_ref) dedupe), so
concurrent MCP + cron + skos-sink writers could lose or corrupt updates.

Three properties are proven:
  1. atomicity      - a write is all-or-nothing; a crash mid-write never leaves
                      a partial file (the old content survives intact).
  2. mutual-exclusion - the MCP path and the skos sink serialize on ONE store
                      lock, so a concurrent write is never lost.
  3. dedupe         - repeat (source, source_ref) is skipped through the MCP
                      capture path, across the whole store (incl. archive).
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    """Redirect the store into tmp_path so tests never touch ~/.skcapstone."""
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))


# ── 1. atomicity ──────────────────────────────────────────────────────────
def test_save_is_atomic_no_partial_on_crash(tmp_path: Path, monkeypatch):
    """A crash mid-write must leave the OLD file intact, never a partial one.

    Fail-before: the old _save_list used path.write_text (in-place truncate),
    so it never called os.replace, did not raise, and overwrote the target.
    Pass-after: the atomic save writes a temp file then os.replace()s it, so a
    failure at the rename step leaves the original content and no temp leftover.
    """
    import os as _os

    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _load_list, _save_list

    _gtd_dir()  # seed store files
    good = [{"id": "keep", "text": "original", "status": "inbox"}]
    _save_list("inbox", good)

    # Simulate a crash: the rename that publishes the new bytes fails.
    def boom(*_a, **_k):
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        _save_list("inbox", [{"id": "new", "text": "half-written", "status": "inbox"}])

    # Read path never calls os.replace, so it is safe with the patch in place.
    assert _load_list("inbox") == good, "target must still hold the old content"
    leftovers = [p.name for p in _gtd_dir().iterdir() if ".tmp" in p.name]
    assert leftovers == [], f"no partial temp files may linger: {leftovers}"


# ── 2. mutual exclusion (no lost concurrent write) ────────────────────────
def test_mcp_and_skos_sink_share_one_store_lock(tmp_path: Path):
    """The MCP path and the skos sink block on the SAME .gtd.lock.

    Fail-before: the old gtd_tools had no _store_lock at all (ImportError), and
    no lock meant an interleaved skos-sink write could clobber an MCP update.
    Pass-after: while the MCP side holds _store_lock(), a skos.capture() blocks
    until it is released, then its write lands intact.
    """
    sink = pytest.importorskip("skos.gtd_ingest")
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _load_list, _store_lock

    _gtd_dir()
    order: list[str] = []
    holding = threading.Event()

    def hold_mcp_lock():
        with _store_lock():
            holding.set()
            time.sleep(0.25)          # keep the lock while the sink tries to write
            order.append("mcp-release")

    t_mcp = threading.Thread(target=hold_mcp_lock)
    t_mcp.start()
    assert holding.wait(2), "MCP lock holder never started"

    def skos_write():
        sink.capture(sink.GtdCapture(
            text="cron: backup failed", source="cron", source_ref="cron:backup@1"))
        order.append("skos-done")

    t_sink = threading.Thread(target=skos_write)
    t_sink.start()
    t_mcp.join(3)
    t_sink.join(3)

    # The sink could only finish AFTER the MCP side released the shared lock.
    assert order == ["mcp-release", "skos-done"], f"lock not mutually exclusive: {order}"
    # And the sink's write was not lost.
    inbox = _load_list("inbox")
    assert any(it.get("source_ref") == "cron:backup@1" for it in inbox)


# ── 3. dedupe by (source, source_ref) through the MCP path ────────────────
def test_capture_dedupes_by_source_ref_through_mcp(tmp_path: Path):
    """Repeat (source, source_ref) is skipped through the MCP capture path.

    Fail-before: the old _handle_gtd_capture always appended (no dedupe), so a
    second identical capture created a duplicate and reported captured=True.
    Pass-after: the second capture is reported captured=False / duplicate=True
    and only one copy exists; a different ref and a no-ref quick-add still land.
    """
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _handle_gtd_capture, _save_archive

    def cap(**kw):
        return json.loads(asyncio.run(_handle_gtd_capture(kw))[0].text)

    a = cap(text="pay invoice", source="email", source_ref="gmail:thread-123")
    assert a["captured"] is True

    b = cap(text="pay invoice (dup)", source="email", source_ref="gmail:thread-123")
    assert b["captured"] is False and b.get("duplicate") is True

    inbox = json.loads((_gtd_dir() / "inbox.json").read_text(encoding="utf-8"))
    assert sum(1 for it in inbox if it.get("source_ref") == "gmail:thread-123") == 1

    # Whole-store dedupe: a ref already sitting in the archive also blocks.
    _save_archive([{"id": "arch1", "text": "old", "source": "email",
                    "source_ref": "gmail:thread-arch", "status": "done"}])
    d = cap(text="re-surfaced", source="email", source_ref="gmail:thread-arch")
    assert d["captured"] is False and d.get("duplicate") is True

    # A different ref is NOT deduped.
    e = cap(text="other", source="email", source_ref="gmail:thread-999")
    assert e["captured"] is True

    # A quick-add without a source_ref is always captured (unchanged behavior).
    q1 = cap(text="quick note")
    q2 = cap(text="quick note")
    assert q1["captured"] is True and q2["captured"] is True
