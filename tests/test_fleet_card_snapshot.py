"""Concurrency contracts for bounded fleet card snapshots."""

from __future__ import annotations

import concurrent.futures
import json

import pytest

from skcapstone.fleet_card_snapshot import acquire_fleet_card_snapshot


def _card(cards, card_id, owner=None):
    card = cards / card_id
    events = card / "events"
    events.mkdir(parents=True)
    (card / "core.json").write_text(
        json.dumps({"id": card_id, "kind": "task", "initial_owner": owner}),
        encoding="utf-8",
    )
    (events / "events.jsonl").write_text(
        json.dumps({"action": "move", "column": "ready", "seq": 1}) + "\n",
        encoding="utf-8",
    )


def test_concurrent_seraph_niobe_and_host_reads_share_exact_generation(tmp_path):
    cards = tmp_path / "cards"
    _card(cards, "11111111")
    _card(cards, "22222222")

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        snapshots = list(executor.map(lambda _seat: acquire_fleet_card_snapshot(cards), range(3)))

    assert {snapshot.generation for snapshot in snapshots} == {snapshots[0].generation}
    assert all(tuple(snapshot.cores) == ("11111111", "22222222") for snapshot in snapshots)


def test_snapshot_is_immutable_and_bound_failures_are_closed(tmp_path):
    cards = tmp_path / "cards"
    _card(cards, "11111111")
    snapshot = acquire_fleet_card_snapshot(cards)

    with pytest.raises(TypeError):
        snapshot.cores["11111111"] = {}
    with pytest.raises(TypeError):
        snapshot.cores["11111111"]["id"] = "changed"
    with pytest.raises(ValueError, match="card bound"):
        acquire_fleet_card_snapshot(cards, max_cards=0)
    with pytest.raises(ValueError, match="byte bound"):
        acquire_fleet_card_snapshot(cards, max_bytes=1)


def test_snapshot_does_not_change_after_live_card_mutation(tmp_path):
    cards = tmp_path / "cards"
    _card(cards, "11111111")
    before = acquire_fleet_card_snapshot(cards)
    _card(cards, "22222222")

    assert tuple(before.cores) == ("11111111",)
    assert acquire_fleet_card_snapshot(cards).generation != before.generation
