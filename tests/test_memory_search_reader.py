"""SKMEM-P0-18: memory_search takes a reader and never widens past it.

Hermetic: runs under ANY installed skmemory (CI pins an old one) by injecting
fake stores, so nothing here skips and nothing touches a real backend.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone import memory_engine
from skcapstone.mcp_tools import memory_tools
from skcapstone.models import MemoryEntry

SECRET = "zebra-quartz private medical note"
PUB = "zebra-quartz team standup is at nine"


class ReaderStore:
    """Fake skmemory store whose search accepts ``reader`` and records it."""

    def __init__(self):
        self.calls = []
        self.rows = [
            SimpleNamespace(id="m-secret", content=SECRET, context_tag="@chef-only"),
            SimpleNamespace(id="m-pub", content=PUB, context_tag="@public"),
        ]

    def search(self, query, limit=10, *, reader=None, **kw):
        self.calls.append({"query": query, "limit": limit, "reader": reader})
        if reader is None:
            return list(self.rows)
        return [r for r in self.rows if r.context_tag == "@public"]


class OldStore:
    """Fake pre-reader skmemory: no ``reader`` parameter."""

    def __init__(self):
        self.calls = 0

    def search(self, query, limit=10):
        self.calls += 1
        return [SimpleNamespace(id="m-secret", content=SECRET, context_tag="@chef-only")]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    monkeypatch.delenv("SKMEMORY_READER", raising=False)
    monkeypatch.setattr(memory_tools, "_home", lambda: Path(tmp_path))
    monkeypatch.setattr("skcapstone.AGENT_HOME", Path(tmp_path))
    monkeypatch.setattr(
        "skcapstone.memory_adapter.memory_to_entry",
        lambda m: MemoryEntry(memory_id=m.id, content=m.content),
    )

    def use(store):
        monkeypatch.setattr(memory_engine, "_get_unified", lambda: store)
        return store

    return use


def _call(args: dict) -> list[str]:
    res = asyncio.run(memory_tools._handle_memory_search(args))
    return [m["content"] for m in json.loads(res[0].text)]


def test_schema_advertises_reader():
    tool = next(t for t in memory_tools.TOOLS if t.name == "memory_search")
    assert "reader" in tool.inputSchema["properties"]


def test_reader_is_passed_through_to_skmemory(wired):
    store = wired(ReaderStore())
    got = _call({"query": "zebra-quartz", "reader": "channel:room1"})
    assert store.calls[0]["reader"] == "channel:room1"
    assert got == [PUB]


def test_undeclared_reader_is_owner_unchanged(wired):
    store = wired(ReaderStore())
    got = _call({"query": "zebra-quartz"})
    assert SECRET in got and store.calls[0]["reader"] is None


def test_env_ceiling_forces_reader_path_and_no_file_fallback(wired, monkeypatch):
    store = wired(ReaderStore())
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    _call({"query": "zebra-quartz"})
    assert len(store.calls) == 1  # went through skmemory (which applies the ceiling)


def test_old_skmemory_fails_closed(wired):
    store = wired(OldStore())
    assert _call({"query": "zebra-quartz", "reader": "@public"}) == []
    assert store.calls == 0  # the unfiltered search never ran


def test_old_skmemory_fails_closed_under_env_ceiling(wired, monkeypatch):
    store = wired(OldStore())
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    assert _call({"query": "zebra-quartz"}) == []
    assert store.calls == 0


def test_search_error_fails_closed(wired):
    class Boom(ReaderStore):
        def search(self, *a, **k):
            raise RuntimeError("db down")

    wired(Boom())
    assert _call({"query": "zebra-quartz", "reader": "@public"}) == []


def test_foreign_home_gets_nothing_for_declared_reader(wired, tmp_path):
    wired(ReaderStore())
    other = tmp_path / "other"
    other.mkdir()
    assert memory_engine.search(other, "zebra-quartz", reader="@public") == []


def test_no_file_fallback_for_declared_reader(wired):
    wired(None)
    assert _call({"query": "zebra-quartz", "reader": "@public"}) == []
