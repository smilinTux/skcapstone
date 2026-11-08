"""Compatibility of skcapstone with the skmemory memory overhaul (0.11.156).

The ChromaDB, SKVector and SKGraph backends and the EndpointSelector were
removed upstream. These tests pin that skcapstone no longer imports them.
"""

from __future__ import annotations

import importlib
import sys
from unittest.mock import MagicMock

import pytest

pytest.importorskip("skmemory")


def test_memory_adapter_store_ignores_retired_env(monkeypatch, tmp_path):
    from skcapstone import memory_adapter

    monkeypatch.setenv("SKMEMORY_SKVECTOR_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("SKMEMORY_SKGRAPH_URL", "redis://127.0.0.1:1")
    monkeypatch.setenv("SKMEMORY_HOME", str(tmp_path / "skmemory"))
    monkeypatch.setenv("SKCAPSTONE_HOME", str(tmp_path / "skcap"))
    store = memory_adapter._get_store()
    assert store is not None
    assert store.vector is None
    assert store.graph is None


def test_retired_index_helpers_return_false():
    from skcapstone.sync_watcher import graph_index_seed, vector_index_seed

    mem = MagicMock()
    mem.id = "m1"
    assert vector_index_seed(mem) is False
    assert graph_index_seed(mem) is False


def test_no_removed_skmemory_modules_imported_by_src():
    removed = (
        "skmemory.backends.skvector_backend",
        "skmemory.backends.skgraph_backend",
        "skmemory.backends.chroma_backend",
        "skmemory.endpoint_selector",
        "skmemory.standing",
        "skmemory.taskqueue",
    )
    for mod in ("skcapstone.memory_adapter", "skcapstone.sync_watcher"):
        importlib.import_module(mod)
    for name in removed:
        assert name not in sys.modules
