import hashlib
import json

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.link_review_work import (
    load_review_work,
    reconcile_review_work,
    reconcile_review_work_batch,
    review_card_id,
)


def _manifest(item):
    data = {
        "schema": "skfleet.link-lineage/v1",
        "source_revision": "1" * 64,
        "coverage": {"unresolved": 1},
        "records": {},
        "reviewer_candidates": [],
        "diagnostics": [],
        "unresolved_prs": [7],
        "review_work_recommendations": [item],
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    data["evidence_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    return data


def _item():
    return {
        "kind": "review-work",
        "reason": "missing_terminal_review",
        "repository": "org/repo",
        "pr": 7,
        "head_revision": "a" * 40,
        "base_revision": "b" * 40,
        "source_card": "source01",
        "card_generation": "2" * 64,
        "source_owner": "builder",
        "reviewer_candidates": [
            {"name": "Seraph", "seat": "seraph", "identity": "seraph", "eligible": True}
        ],
    }


def _home_with_source(tmp_path, name=".skcapstone"):
    home = tmp_path / name
    home.mkdir()
    CardStore(home).create(
        CardCore(
            id="source01",
            title="Source",
            created_by="builder",
            created_at="2026-09-07T00:00:00+00:00",
        )
    )
    return home


def test_loads_exact_review_work_from_signed_manifest(tmp_path):
    path = tmp_path / "lineage.json"
    path.write_text(json.dumps(_manifest(_item())))
    revision, evidence, work = load_review_work(path)
    assert revision == "1" * 64
    assert len(evidence) == 64
    assert work == [_item()]


def test_rejects_non_distinct_reviewer_and_tampering(tmp_path):
    item = _item()
    item["source_owner"] = "seraph"
    path = tmp_path / "lineage.json"
    path.write_text(json.dumps(_manifest(item)))
    with pytest.raises(ValueError, match="not distinct"):
        load_review_work(path)
    data = _manifest(_item())
    data["source_revision"] = "3" * 64
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="evidence invalid"):
        load_review_work(path)


@pytest.mark.parametrize("source_card", [None, "bad/id", ""])
def test_rejects_missing_or_path_like_source_card(tmp_path, source_card):
    item = _item()
    if source_card is None:
        item.pop("source_card")
    else:
        item["source_card"] = source_card
    path = tmp_path / "lineage.json"
    path.write_text(json.dumps(_manifest(item)))

    with pytest.raises(ValueError, match="source card invalid"):
        load_review_work(path)


def test_reconcile_is_restart_idempotent_and_launchable(tmp_path):
    home = _home_with_source(tmp_path)
    item = _item()
    first = reconcile_review_work(home, item, evidence_sha256="4" * 64)
    second = reconcile_review_work(home, item, evidence_sha256="4" * 64)

    assert first.created is True
    assert second.created is False
    assert first.launchable is second.launchable is True
    assert first.review_card_id == second.review_card_id
    card = CardStore(home).fold(first.review_card_id)
    assert card is not None
    assert card.owner is None
    assert card.status.value == "backlog"
    assert "review" in card.labels
    assert card.links["producer_identity"] == "builder"


def test_reconcile_converges_for_cross_host_order(tmp_path):
    homes = [_home_with_source(tmp_path, host) for host in ("chiap01", "chiap08")]
    item = _item()
    results = [reconcile_review_work(home, dict(item), evidence_sha256="5" * 64) for home in homes]

    expected = review_card_id("source01", "a" * 40)
    assert {result.review_card_id for result in results} == {expected}
    cores = [(home / "cards" / expected / "core.json").read_bytes() for home in homes]
    assert cores[0] == cores[1]


def test_new_head_gets_distinct_review_card(tmp_path):
    home = _home_with_source(tmp_path)
    first = reconcile_review_work(home, _item(), evidence_sha256="6" * 64)
    changed = _item()
    changed["head_revision"] = "c" * 40
    second = reconcile_review_work(home, changed, evidence_sha256="6" * 64)

    assert first.review_card_id != second.review_card_id


def test_ready_review_card_reproduces_reviewer_preflight_rejection(tmp_path):
    home = _home_with_source(tmp_path)
    store = CardStore(home)
    card_id = review_card_id("source01", "a" * 40)
    store.create(
        CardCore(
            id=card_id,
            title="[REVIEW] exact head",
            initial_labels=["review", "parent-source01"],
            meta={
                "link_source_card": "source01",
                "link_head_revision": "a" * 40,
            },
        )
    )
    store.append_event(card_id, "move", "coordinator", column="ready")

    result = reconcile_review_work(home, _item(), evidence_sha256="6" * 64)

    assert result.launchable is False
    assert result.reason == "review card is not unclaimed review work"


def test_duplicate_matching_cards_fail_closed(tmp_path):
    home = _home_with_source(tmp_path)
    store = CardStore(home)
    metadata = {
        "link_source_card": "source01",
        "link_head_revision": "a" * 40,
    }
    store.create(CardCore(id="duplicate1", title="one", meta=metadata))
    store.create(CardCore(id="duplicate2", title="two", meta=metadata))

    result = reconcile_review_work(home, _item(), evidence_sha256="7" * 64)

    assert result.launchable is False
    assert result.reason == "duplicate_review_cards"


def test_batch_scans_existing_cards_once(tmp_path, monkeypatch):
    home = _home_with_source(tmp_path)
    calls = 0
    original = CardStore.list_cards

    def counted(store, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(store, *args, **kwargs)

    monkeypatch.setattr(CardStore, "list_cards", counted)
    results = reconcile_review_work_batch(home, [_item(), _item()], evidence_sha256="8" * 64)

    # One batch index plus the governed create's own safety scan. The repeated
    # recommendation does not trigger another full scan.
    assert calls == 2
    assert [result.created for result in results] == [True, False]
    assert all(result.launchable for result in results)


def test_noncanonical_manual_card_fails_closed(tmp_path):
    home = _home_with_source(tmp_path)
    CardStore(home).create(
        CardCore(
            id="manual01",
            title="manual",
            meta={
                "link_source_card": "source01",
                "link_head_revision": "a" * 40,
            },
        )
    )

    result = reconcile_review_work(home, _item(), evidence_sha256="9" * 64)

    assert result.launchable is False
    assert result.reason == "noncanonical_review_card"


def test_canonical_card_with_wrong_parent_fails_closed(tmp_path):
    home = _home_with_source(tmp_path)
    store = CardStore(home)
    store.create(CardCore(id="other", title="Other source"))
    card_id = review_card_id("source01", "a" * 40)
    store.create(
        CardCore(
            id=card_id,
            title="manual",
            initial_labels=["review", "parent-other"],
            meta={
                "link_source_card": "source01",
                "link_head_revision": "a" * 40,
            },
        )
    )

    result = reconcile_review_work(home, _item(), evidence_sha256="a" * 64)

    assert result.launchable is False
    assert result.reason == "review_parent_invalid"


def test_changed_recommendation_fails_closed_without_duplicate_event(tmp_path):
    home = _home_with_source(tmp_path)
    first = reconcile_review_work(home, _item(), evidence_sha256="b" * 64)
    changed = _item()
    changed["reviewer_candidates"][0]["identity"] = "seraph-two"
    changed["reviewer_candidates"][0]["name"] = "seraph-two"

    second = reconcile_review_work(home, changed, evidence_sha256="c" * 64)

    assert first.launchable is True
    assert second.launchable is False
    assert second.reason == "review recommendation is not recorded exactly"
    events = CardStore(home)._read_events(first.review_card_id)
    assert sum(event["action"] == "review_assignment_recommendation" for event in events) == 1
