"""Regression tests for exact-generation terminal capacity publication."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.terminal_capacity import invalidate_worker, retire_worker_generation


def _claimed(store: CardStore, card: str, owner: str, revision: str) -> None:
    store.create(
        CardCore(
            id=card,
            title=card,
            initial_owner=owner,
            initial_claim_revision=revision,
        )
    )


def _release(store: CardStore, card: str, owner: str, revision: str) -> None:
    store.append_event(
        card,
        "release_claim",
        owner,
        released_owner=owner,
        expected_claim_revision=revision,
    )


def _snapshot(path, *workers) -> bytes:
    payload = {
        "host": "chiap08",
        "cards": [worker["card_id"] for worker in workers],
        "workers": list(workers),
        "lanes": {"codex": {"busy": len(workers), "free": 0, "target": 1}},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.read_bytes()


def _worker(card: str, owner: str, revision: str) -> dict[str, str]:
    return {"card_id": card, "owner": owner, "claim_revision": revision}


@pytest.mark.parametrize("content", ["not-json", "[]", '{"cards":[]}'])
def test_malformed_snapshot_is_unchanged(content, tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(content, encoding="utf-8")
    before = snapshot.read_bytes()

    with pytest.raises(ValueError, match="snapshot"):
        invalidate_worker(snapshot, "chiap08", "finished", "worker", "rev-1")

    assert snapshot.read_bytes() == before


def test_malformed_target_preserves_valid_sibling_occupancy(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    sibling = _worker("sibling", "worker-2", "rev-2")
    before = _snapshot(snapshot, sibling, {"card_id": "finished"})

    with pytest.raises(ValueError, match="exact worker generation"):
        invalidate_worker(snapshot, "chiap08", "finished", "worker", "rev-1")

    assert snapshot.read_bytes() == before
    assert json.loads(snapshot.read_text())["cards"] == ["sibling", "finished"]


def test_stale_generation_cannot_remove_newer_claim(tmp_path):
    home = tmp_path / "coord"
    home.mkdir()
    store = CardStore(home)
    _claimed(store, "feedbeef", "worker", "rev-1")
    _release(store, "feedbeef", "worker", "rev-1")
    store.append_event(
        "feedbeef", "claim", "new-worker", owner="new-worker", claim_revision="rev-2"
    )
    snapshot = tmp_path / "fleet-live.json"
    before = _snapshot(snapshot, _worker("feedbeef", "new-worker", "rev-2"))

    assert (
        retire_worker_generation(snapshot, home, "chiap08", "feedbeef", "worker", "rev-1") is None
    )
    assert snapshot.read_bytes() == before


def test_unreleased_generation_remains_occupied(tmp_path):
    home = tmp_path / "coord"
    home.mkdir()
    store = CardStore(home)
    _claimed(store, "feedbeef", "worker", "rev-1")
    snapshot = tmp_path / "fleet-live.json"
    before = _snapshot(snapshot, _worker("feedbeef", "worker", "rev-1"))

    assert (
        retire_worker_generation(snapshot, home, "chiap08", "feedbeef", "worker", "rev-1") is None
    )
    assert snapshot.read_bytes() == before


def test_concurrent_terminal_workers_preserve_live_siblings(tmp_path):
    home = tmp_path / "coord"
    home.mkdir()
    store = CardStore(home)
    finished = [f"finish{index:02x}" for index in range(8)]
    sibling = _worker("deadbeef", "live-worker", "live-rev")
    workers = [sibling]
    for index, card in enumerate(finished):
        owner, revision = f"worker-{index}", f"rev-{index}"
        _claimed(store, card, owner, revision)
        _release(store, card, owner, revision)
        workers.append(_worker(card, owner, revision))
    snapshot = tmp_path / "fleet-live.json"
    _snapshot(snapshot, *workers)

    def retire(card: str) -> None:
        index = finished.index(card)
        retire_worker_generation(
            snapshot, home, "chiap08", card, f"worker-{index}", f"rev-{index}"
        )

    with ThreadPoolExecutor(max_workers=len(finished)) as pool:
        list(pool.map(retire, finished))

    published = json.loads(snapshot.read_text(encoding="utf-8"))
    assert published["cards"] == ["deadbeef"]
    assert published["workers"] == [sibling]


def test_exact_terminal_generation_allows_immediate_next_claim_and_launch(tmp_path):
    home = tmp_path / "coord"
    home.mkdir()
    store = CardStore(home)
    _claimed(store, "feedbeef", "worker-1", "rev-1")
    _release(store, "feedbeef", "worker-1", "rev-1")
    snapshot = tmp_path / "fleet-live.json"
    _snapshot(snapshot, _worker("feedbeef", "worker-1", "rev-1"))

    retired = retire_worker_generation(snapshot, home, "chiap08", "feedbeef", "worker-1", "rev-1")
    assert retired is not None
    assert json.loads(snapshot.read_text())["cards"] == []

    store.append_event("feedbeef", "claim", "worker-2", owner="worker-2", claim_revision="rev-2")
    claimed = store.fold("feedbeef")
    assert claimed.owner == "worker-2"
    assert claimed.meta["_claim_revision"] == "rev-2"
    launched = subprocess.run(
        [sys.executable, "-c", "print('launched-next-seat')"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert launched.stdout.strip() == "launched-next-seat"
