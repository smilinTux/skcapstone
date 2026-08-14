"""SPE P1.1: every GTD mutation appends one event to a per-writer journal.

Card ``3d927cda`` (sprint ``83482526``, epic ``373a33ca``). The GTD store had no
history at all: state was the flat JSON lists and nothing else, so an item that
moved (or vanished) left no record of who moved it, from where, or when. That is
the substrate both reopen (P1.2) and attribution (P2) need.

The shape is copied from the CardStore, which has run this pattern since the
July-13 refactor: append-only ``<writer>@<host>.jsonl`` files, one per writer so
no two processes ever write the same file, ordered by ``(ts, writer, seq)`` on
read. Every event carries the item's POST-state and its destination list, so a
fold is "drop this id everywhere, then put it where the event says", which
reproduces the live lists exactly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    monkeypatch.setenv("SKAGENT", "lumina")


def _capture(text: str, **kw) -> dict:
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_capture

    return json.loads(asyncio.run(_handle_gtd_capture({"text": text, **kw}))[0].text)


def _events() -> list[dict]:
    from skcapstone import gtd_journal

    return gtd_journal.read_all()


def test_capture_appends_exactly_one_event():
    from skcapstone import gtd_journal

    result = _capture("buy milk")
    events = _events()
    assert len(events) == 1
    ev = events[0]
    assert ev["action"] == "capture"
    assert ev["item_id"] == result["id"]
    assert ev["to"] == "inbox"
    assert ev["item"]["text"] == "buy milk"
    assert ev["writer"] == "lumina"
    assert ev["node"] and ev["ts"] and ev["event_id"]
    assert gtd_journal.journal_dir().exists()


def test_each_writer_owns_its_own_file(monkeypatch):
    """Per-writer files are what makes this conflict-free under Syncthing."""
    from skcapstone import gtd_journal

    _capture("from lumina")
    monkeypatch.setenv("SKAGENT", "opus")
    _capture("from opus")

    names = sorted(p.name for p in gtd_journal.journal_dir().glob("*.jsonl"))
    assert len(names) == 2
    assert any(n.startswith("lumina@") for n in names)
    assert any(n.startswith("opus@") for n in names)


def test_seq_is_per_writer_and_monotonic():
    _capture("one")
    _capture("two")
    _capture("three")
    assert [e["seq"] for e in _events()] == [0, 1, 2]


def test_move_appends_one_event_naming_both_ends():
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_move

    item_id = _capture("do the thing")["id"]
    asyncio.run(_handle_gtd_move({"item_id": item_id, "destination": "next"}))

    ev = _events()[-1]
    assert ev["action"] == "move"
    assert ev["item_id"] == item_id
    assert ev["from"] == "inbox"
    assert ev["to"] == "next-actions"
    assert ev["item"]["status"] == "next"


def test_done_appends_one_event_pointing_at_the_archive():
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_done

    item_id = _capture("finish it")["id"]
    asyncio.run(_handle_gtd_done({"item_id": item_id}))

    ev = _events()[-1]
    assert ev["action"] == "done"
    assert ev["from"] == "inbox"
    assert ev["to"] == "archive"
    assert ev["item"]["completed_at"]


def test_clarify_appends_one_event():
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_clarify

    item_id = _capture("ambiguous thing")["id"]
    asyncio.run(_handle_gtd_clarify({"item_id": item_id, "actionable": True, "two_minute": False}))

    ev = _events()[-1]
    assert ev["action"] == "clarify"
    assert ev["from"] == "inbox"
    assert ev["to"] == "next-actions"


def test_a_failed_mutation_appends_nothing():
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_done

    result = json.loads(asyncio.run(_handle_gtd_done({"item_id": "nope"}))[0].text)
    assert result.get("error")
    assert _events() == []


def test_fold_reproduces_the_live_lists():
    from skcapstone import gtd_journal
    from skcapstone.mcp_tools.gtd_tools import (
        GTD_FILES,
        _handle_gtd_done,
        _handle_gtd_move,
        _load_list,
    )

    a = _capture("stays in inbox")["id"]
    b = _capture("becomes an action")["id"]
    c = _capture("gets finished")["id"]
    asyncio.run(_handle_gtd_move({"item_id": b, "destination": "next"}))
    asyncio.run(_handle_gtd_done({"item_id": c}))

    folded = gtd_journal.fold()
    live = {name: _load_list(name) for name in GTD_FILES}

    assert {k: [it["id"] for it in v] for k, v in folded.items()} == {
        k: [it["id"] for it in v] for k, v in live.items()
    }
    assert folded["inbox"][0]["id"] == a
    assert folded["next-actions"][0]["id"] == b
    assert folded["archive"][0]["id"] == c


def test_fold_is_deterministic_across_writers(monkeypatch):
    """Two writers, interleaved: the fold orders by (ts, writer, seq), not by
    whichever file the directory listing happens to return first."""
    from skcapstone import gtd_journal
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_move

    a = _capture("first")["id"]
    monkeypatch.setenv("SKAGENT", "opus")
    _capture("second")
    monkeypatch.setenv("SKAGENT", "lumina")
    asyncio.run(_handle_gtd_move({"item_id": a, "destination": "waiting"}))

    once = gtd_journal.fold()
    twice = gtd_journal.fold()
    assert once == twice
    assert [it["id"] for it in once["waiting-for"]] == [a]


def test_the_journal_is_append_only():
    """A second mutation must never rewrite the first line."""
    from skcapstone import gtd_journal
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_move

    item_id = _capture("one")["id"]
    path = next(gtd_journal.journal_dir().glob("*.jsonl"))
    first_line = path.read_text(encoding="utf-8").splitlines()[0]

    asyncio.run(_handle_gtd_move({"item_id": item_id, "destination": "someday"}))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == first_line


def test_a_corrupt_line_does_not_sink_the_fold():
    from skcapstone import gtd_journal

    _capture("good one")
    path = next(gtd_journal.journal_dir().glob("*.jsonl"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json at all\n")

    assert len(gtd_journal.read_all()) == 1
    assert len(gtd_journal.fold()["inbox"]) == 1
