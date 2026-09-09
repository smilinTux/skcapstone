"""Regression tests for exact terminal capacity publication fencing."""

import json

import pytest

from skcapstone.fleet.terminal_capacity import invalidate_worker


def _snapshot(card="worker", revision="rev-1"):
    return {
        "host": "chiap08",
        "cards": [card],
        "generations": {
            card: {
                "card_id": card,
                "owner": "agent",
                "claim_revision": revision,
            }
        },
    }


def test_stale_generation_cannot_release_reused_card(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    original = _snapshot(revision="rev-new")
    snapshot.write_text(json.dumps(original) + "\n", encoding="utf-8")

    published = invalidate_worker(
        snapshot,
        "chiap08",
        "worker",
        owner="agent",
        card_id="worker",
        claim_revision="rev-old",
        process_evidence=True,
        cgroup_evidence=True,
    )

    assert published == original
    assert json.loads(snapshot.read_text(encoding="utf-8")) == original


def test_malformed_snapshot_is_preserved(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    original = "not-json\n"
    snapshot.write_text(original, encoding="utf-8")

    published = invalidate_worker(snapshot, "chiap08", "worker")

    assert published == {}
    assert snapshot.read_text(encoding="utf-8") == original


@pytest.mark.parametrize(
    ("process_evidence", "cgroup_evidence"),
    [(False, True), (True, False), (False, False), (None, True), (True, None)],
)
def test_surviving_process_or_cgroup_keeps_seat_occupied(
    tmp_path, process_evidence, cgroup_evidence
):
    snapshot = tmp_path / "fleet-live.json"
    original = _snapshot()
    snapshot.write_text(json.dumps(original) + "\n", encoding="utf-8")

    published = invalidate_worker(
        snapshot,
        "chiap08",
        "worker",
        owner="agent",
        card_id="worker",
        claim_revision="rev-1",
        process_evidence=process_evidence,
        cgroup_evidence=cgroup_evidence,
    )

    assert published == original
    assert json.loads(snapshot.read_text(encoding="utf-8")) == original


def test_exact_process_and_cgroup_reconciliation_allows_immediate_safe_reuse(tmp_path):
    snapshot = tmp_path / "fleet-live.json"
    snapshot.write_text(json.dumps(_snapshot()) + "\n", encoding="utf-8")

    released = invalidate_worker(
        snapshot,
        "chiap08",
        "worker",
        owner="agent",
        card_id="worker",
        claim_revision="rev-1",
        process_evidence=True,
        cgroup_evidence=True,
    )
    assert released["cards"] == []
    assert "worker" not in released["generations"]

    reused = _snapshot(revision="rev-2")
    snapshot.write_text(json.dumps(reused) + "\n", encoding="utf-8")
    published = invalidate_worker(
        snapshot,
        "chiap08",
        "worker",
        owner="agent",
        card_id="worker",
        claim_revision="rev-2",
        process_evidence=True,
        cgroup_evidence=True,
    )

    assert published["cards"] == []
    assert "worker" not in published["generations"]
