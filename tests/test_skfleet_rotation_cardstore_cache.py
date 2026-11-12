"""The rotation reuses validated CardStore event reads within one cycle."""

from __future__ import annotations

import ast
import os
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_cache_helper():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = {
        item.name: item
        for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name in {"_cache_card_store_event_reads", "_strict_card_events"}
    }
    assert set(nodes) == {"_cache_card_store_event_reads", "_strict_card_events"}
    namespace = {
        "CardStore": CardStore,
        "Path": Path,
        "HOME": Path("/unused"),
        "CARDS": "/unused/cards",
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "_production_native_store": None,
        "_claim_rows": {},
        "os": os,
    }
    module = ast.Module(body=list(nodes.values()), type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_native_fold_reuses_one_validated_event_read_per_card(tmp_path, monkeypatch):
    """The authority fold and native fold share bytes without weakening validation."""
    card_id = "a1b2c3d4"
    home = tmp_path / ".skcapstone"
    (home / "coordination" / "locks").mkdir(parents=True)
    CardStore(home).create(CardCore(id=card_id, title="cache test"))

    reads = []
    original = CardStore._read_events

    def count_reads(store, current_id):
        reads.append(current_id)
        return original(store, current_id)

    monkeypatch.setattr(CardStore, "_read_events", count_reads)
    namespace = _load_cache_helper()
    namespace["HOME"] = tmp_path
    namespace["CARDS"] = str(home / "cards")
    namespace["_strict_card_events"](card_id)
    store = namespace["_production_native_store"]
    assert store.fold(card_id) is not None
    assert reads == [card_id]

    reads.clear()
    CardStore(home).append_event(card_id, "note", "writer", text="fresh event")
    reads.clear()
    fresh_rows = namespace["_strict_card_events"](card_id, fresh=True)
    fresh_store = namespace["_production_native_store"]
    assert fresh_rows[-1]["text"] == "fresh event"
    assert fresh_store.fold(card_id) is not None
    assert reads == [card_id]
