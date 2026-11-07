"""SKMEM-P0-18: memory_search takes a reader and never widens past it."""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest

skmemory = pytest.importorskip("skmemory")
from skmemory.store import MemoryStore  # noqa: E402

if "reader" not in inspect.signature(MemoryStore.search).parameters:
    pytest.skip("installed skmemory predates search(reader=)", allow_module_level=True)

from skmemory.backends.sqlite_backend import SQLiteBackend  # noqa: E402
from skmemory.models import Memory, MemoryLayer  # noqa: E402

from skcapstone import memory_engine  # noqa: E402
from skcapstone.mcp_tools import memory_tools  # noqa: E402

SECRET = "zebra-quartz private medical note"
PUB = "zebra-quartz team standup is at nine"


@pytest.fixture
def store(tmp_path, monkeypatch):
    for var in ("SKMEMORY_READER", "SKMEMORY_PG_DSN", "SKMEMORY_VECTOR_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SKMEMORY_EMBED_URL", "http://127.0.0.1:1/api/embed")
    base = tmp_path / "mem"
    base.mkdir()
    s = MemoryStore(primary=SQLiteBackend(base_path=str(base)), vector=None, graph=None)
    for mid, text, tag in (("m-secret", SECRET, "@chef-only"), ("m-pub", PUB, "@public")):
        s.primary.save(
            Memory(id=mid, title=text[:20], content=text, layer=MemoryLayer.LONG, context_tag=tag)
        )
    monkeypatch.setattr(memory_engine, "_get_unified", lambda: s)
    monkeypatch.setattr(memory_tools, "_home", lambda: Path(tmp_path))
    monkeypatch.setattr("skcapstone.AGENT_HOME", Path(tmp_path))
    return s


def _call(args: dict) -> list[str]:
    res = asyncio.run(memory_tools._handle_memory_search(args))
    return [m["content"] for m in json.loads(res[0].text)]


def test_schema_advertises_reader():
    tool = next(t for t in memory_tools.TOOLS if t.name == "memory_search")
    assert "reader" in tool.inputSchema["properties"]


def test_public_reader_cannot_see_chef_only(store):
    got = _call({"query": "zebra-quartz", "reader": "@public"})
    assert SECRET not in got and any(PUB in c for c in got)


def test_undeclared_reader_is_owner(store):
    got = _call({"query": "zebra-quartz"})
    assert any(SECRET in c for c in got)


def test_unknown_channel_and_garbage_reader_are_public(store):
    for r in ("channel:no-such-room", "@bogus", ""):
        assert SECRET not in _call({"query": "zebra-quartz", "reader": r})


def test_env_ceiling_not_widened_by_call(store, monkeypatch):
    monkeypatch.setenv("SKMEMORY_READER", "@public")
    assert SECRET not in _call({"query": "zebra-quartz", "reader": "@chef-only"})
    assert SECRET not in _call({"query": "zebra-quartz"})


def test_old_skmemory_fails_closed(store, monkeypatch):
    class Old:
        def search(self, query, limit=10):
            raise AssertionError("unfiltered search must not run")

    monkeypatch.setattr(memory_engine, "_get_unified", lambda: Old())
    assert _call({"query": "zebra-quartz", "reader": "@public"}) == []


def test_foreign_home_gets_nothing_for_declared_reader(store, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    assert memory_engine.search(other, "zebra-quartz", reader="@public") == []


def test_no_file_fallback_for_declared_reader(store, tmp_path, monkeypatch):
    monkeypatch.setattr(memory_engine, "_get_unified", lambda: None)
    assert _call({"query": "zebra-quartz", "reader": "@public"}) == []
