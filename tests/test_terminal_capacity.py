"""Regression tests for atomic terminal capacity publication."""

import json
from concurrent.futures import ThreadPoolExecutor

from skcapstone.fleet.terminal_capacity import invalidate_worker


def test_stale_or_malformed_snapshot_fails_safe_to_empty_capacity(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text("not-json", encoding="utf-8")

    published = invalidate_worker(snapshot, "chiap08", "finished")

    assert published["cards"] == []
    assert published["invalidated_card"] == "finished"
    assert json.loads(snapshot.read_text(encoding="utf-8")) == published


def test_concurrent_terminal_workers_preserve_live_siblings(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    finished = [f"finished-{index}" for index in range(8)]
    snapshot.write_text(
        json.dumps({"host": "chiap08", "cards": ["sibling", *finished]}),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=len(finished)) as pool:
        list(pool.map(lambda card: invalidate_worker(snapshot, "chiap08", card), finished))

    published = json.loads(snapshot.read_text(encoding="utf-8"))
    assert published["cards"] == ["sibling"]
    assert published["invalidated_card"] in finished
