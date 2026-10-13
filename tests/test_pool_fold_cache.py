"""Incremental pool fold: cache hit/miss/invalidation, equivalence, deadline."""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

from skcapstone.card_store import CardCore, CardStore
from skcapstone.fleet import pool_fold_cache as pfc


def _board(tmp_path: Path, count: int = 12) -> tuple[Path, CardStore]:
    home = tmp_path / ".skcapstone"
    store = CardStore(home)
    store.ensure_dirs()
    for index in range(count):
        cid = "%08x" % (0xAB000000 + index)
        core = CardCore(id=cid, title="card %d" % index, created_by="t")
        (home / "cards" / cid).mkdir(parents=True, exist_ok=True)
        (home / "cards" / cid / "core.json").write_text(core.model_dump_json())
        events = home / "cards" / cid / "events"
        events.mkdir(exist_ok=True)
        (events / "w.jsonl").write_text(
            json.dumps({"action": "move", "column": "ready", "ts": "2026-01-01T00:00:0%dZ" % (index % 9),
                        "writer": "w", "seq": 1}) + "\n")
    return home, store


def _later():
    return time.time_ns() + 60 * 10**9


def _old(home: Path) -> None:
    past = 1_000_000_000
    for path in home.rglob("*"):
        os.utime(path, (past, past))


def _dump(cards):
    return [c.model_dump(mode="json") for c in cards]


def test_index_equals_list_cards_and_reuses(tmp_path):
    home, store = _board(tmp_path)
    _old(home)
    index = pfc.CardIndex(home, tmp_path / "c.json", clock=_later)
    assert _dump(index.cards(store)) == _dump(CardStore(home).list_cards())
    assert index.refolded == 12
    index.cards(store)
    assert index.refolded == 12 and index.reused >= 12


def test_append_and_new_card_invalidate(tmp_path):
    home, store = _board(tmp_path)
    _old(home)
    index = pfc.CardIndex(home, tmp_path / "c.json", clock=_later)
    index.cards(store)
    target = home / "cards" / "ab000003" / "events" / "w.jsonl"
    with open(target, "a") as handle:
        handle.write(json.dumps({"action": "move", "column": "done", "ts": "2026-02-01T00:00:00Z",
                                 "writer": "w", "seq": 2}) + "\n")
    got = index.cards(store)
    assert index.refolded == 13
    assert _dump(got) == _dump(CardStore(home).list_cards())
    (home / "cards" / "ab000003" / "events" / "x.jsonl").write_text("")
    index.cards(store)
    assert index.refolded == 14


def test_persisted_cache_round_trip_and_discard(tmp_path):
    home, store = _board(tmp_path)
    _old(home)
    path = tmp_path / "c.json"
    first = pfc.CardIndex(home, path, clock=_later)
    first.cards(store)
    assert first.save()
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    second = pfc.CardIndex(home, path, clock=_later, rng=random.Random(1))
    assert _dump(second.cards(store)) == _dump(CardStore(home).list_cards())
    assert second.refolded == 0 and second.audit_failures == 0
    path.write_text("{not json")
    assert pfc.CardIndex(home, path).cache.loaded == 0
    assert pfc.CardIndex(home, path).cache.loaded == 0


def test_racy_files_are_not_cached(tmp_path):
    home, store = _board(tmp_path)  # fresh mtimes: racy
    index = pfc.CardIndex(home, tmp_path / "c.json")
    index.cards(store)
    index.cards(store)
    assert index.refolded == 24


def test_symlink_falls_back(tmp_path):
    home, store = _board(tmp_path)
    (home / "cards" / "zz-link").symlink_to(home / "cards" / "ab000001")
    index = pfc.CardIndex(home)
    try:
        index.cards(store)
    except ValueError:
        pass
    assert index.fallbacks == 1


def test_audit_detects_stale_entry(tmp_path):
    home, store = _board(tmp_path)
    _old(home)
    path = tmp_path / "c.json"
    first = pfc.CardIndex(home, path, clock=_later)
    first.cards(store)
    first.save()
    raw = json.loads(path.read_text())
    for cid, (key, blob) in raw["entries"].items():
        data = json.loads(blob)
        data["card"]["title"] = "TAMPERED"
        raw["entries"][cid] = [key, json.dumps(data)]
    path.write_text(json.dumps(raw))
    second = pfc.CardIndex(home, path, clock=_later)
    got = second.cards(store)
    assert second.audit_failures == 1
    assert _dump(got) == _dump(CardStore(home).list_cards())


def test_disabled_by_default_and_switch(tmp_path, monkeypatch):
    home, store = _board(tmp_path)
    pfc.disable()
    assert _dump(pfc.sibling_cards(store)) == _dump(store.list_cards())
    monkeypatch.setenv("SKFLEET_POOL_FOLD_CACHE", "0")
    assert pfc.enable(home) is None


def test_deadline_and_cursor(tmp_path):
    assert pfc.soft_deadline_seconds(250) == 150.0
    assert pfc.soft_deadline_seconds(None) is None
    assert pfc.soft_deadline_seconds(250, "0") is None
    assert pfc.soft_deadline_seconds(250, "30") == 30.0
    paths = ["/c/%s" % n for n in ("a1", "b2", "c3", "d4")]
    assert pfc.rotate_from_cursor(paths, None) == paths
    assert pfc.rotate_from_cursor(paths, "c3") == paths[2:] + paths[:2]
    assert pfc.rotate_from_cursor(paths, "zz") == paths
    cursor = tmp_path / "cur.json"
    assert pfc.read_cursor(cursor) is None
    pfc.write_cursor(cursor, "c3")
    assert pfc.read_cursor(cursor) == "c3"
    pfc.write_cursor(cursor, None)
    assert pfc.read_cursor(cursor) is None
    pfc.write_cursor(cursor, None)  # idempotent
