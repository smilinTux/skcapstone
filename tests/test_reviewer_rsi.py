"""Reviewer RSI projection contract tests."""

from __future__ import annotations

from skcoord.card_store import CardCore, CardStore

from skcapstone.reviewer_rsi import project_from_card_store

HASH_A = "a" * 64
HASH_B = "b" * 64


def _card(store: CardStore, card_id: str, *, labels: list[str], meta: dict | None = None) -> None:
    """Create one immutable test card."""
    store.create(
        CardCore(
            id=card_id,
            title=f"Card {card_id}",
            created_by="fixture",
            initial_labels=labels,
            meta=meta or {},
        )
    )


def _attempt(
    store: CardStore,
    card_id: str,
    verdict: str,
    *,
    bucket: str = "sk-m",
    review_key: str | None = None,
    reproduced: bool | None = None,
    reason: str = "",
) -> None:
    """Create one review attempt with hashed verdict evidence."""
    _card(store, card_id, labels=["review", bucket])
    store.append_event(
        card_id, "claim", "fixture", owner="fixture", ts="2026-09-01T00:00:00+00:00"
    )
    payload = {
        "verdict": verdict,
        "evidence_sha256": HASH_A,
        "review_key": review_key or card_id,
        "reason": reason,
        "ts": "2026-09-01T00:01:00+00:00",
    }
    if reproduced is not None:
        payload["reproduced"] = reproduced
    store.append_event(card_id, "verdict", "fixture", **payload)


def _finding(
    store: CardStore,
    card_id: str,
    review_card: str,
    verdict: str,
    *,
    failure_class: str = "",
    owner_card_id: str = "",
) -> None:
    """Create one hashed successor finding."""
    _card(store, card_id, labels=["repair", "sk-m"])
    store.append_event(
        card_id,
        "review_finding",
        "fixture",
        source_review_card=review_card,
        verdict=verdict,
        failure_class=failure_class,
        owner_card_id=owner_card_id,
        evidence_sha256=HASH_B,
        ts="2026-09-01T00:02:00+00:00",
    )


def test_sparse_history_is_explicitly_unavailable(tmp_path) -> None:
    """Sparse review history must never manufacture healthy zeroes."""
    store = CardStore(tmp_path)
    _card(store, "review01", labels=["review", "sk-s"])
    store.append_event("review01", "observation", "fixture", ts="not-a-timestamp")

    report = project_from_card_store(tmp_path)

    bucket = report["buckets"]["sk-s"]
    assert bucket["unavailable"] == {"missing_verdict": 1}
    assert bucket["first_pass_accuracy"]["state"] == "unavailable"
    assert report["observation_window"] == {
        "state": "unavailable",
        "start": None,
        "end": None,
    }


def test_conflicting_and_superseded_reviews_are_not_scored(tmp_path) -> None:
    """Conflicting and superseded review generations are unavailable."""
    store = CardStore(tmp_path)
    _attempt(store, "review01", "PASS")
    store.append_event("review01", "verdict", "fixture2", verdict="FAIL", evidence_sha256=HASH_B)
    _attempt(store, "review02", "PASS")
    store.append_event("review02", "supersede", "fixture", successor="review03")

    bucket = project_from_card_store(tmp_path)["buckets"]["sk-m"]

    assert bucket["available_attempts"] == 0
    assert bucket["unavailable"] == {"conflicting_verdicts": 1, "superseded_review": 1}


def test_successful_first_pass_and_reproducibility(tmp_path) -> None:
    """A confirmed first-pass result contributes to quality metrics."""
    store = CardStore(tmp_path)
    _attempt(store, "review01", "PASS", reproduced=True)
    _finding(store, "zfinding1", "review01", "PASS")

    report = project_from_card_store(tmp_path)
    bucket = report["buckets"]["sk-m"]

    assert bucket["first_pass_accuracy"] == {
        "state": "available",
        "numerator": 1,
        "denominator": 1,
        "value": 1.0,
    }
    assert bucket["reproducibility"]["value"] == 1.0
    assert bucket["latency_seconds"]["median"] == 60.0


def test_false_results_duplicates_stale_blocks_and_recurrence(tmp_path) -> None:
    """Failure classes and operational review defects remain distinct."""
    store = CardStore(tmp_path)
    _card(store, "owner001", labels=["repair-owner", "sk-m"])
    _attempt(store, "review01", "PASS", review_key="same")
    _finding(
        store,
        "finding1",
        "review01",
        "FAIL_CLOSED",
        failure_class="missing-hash",
        owner_card_id="owner001",
    )
    _attempt(store, "review02", "BLOCKED", review_key="same", reason="stale feed")
    _finding(
        store,
        "finding2",
        "review02",
        "PASS",
        failure_class="missing-hash",
        owner_card_id="owner001",
    )

    bucket = project_from_card_store(tmp_path)["buckets"]["sk-m"]

    assert bucket["false_pass"]["numerator"] == 1
    assert bucket["false_fail_closed"]["numerator"] == 1
    assert bucket["duplicate_assignments"] == 1
    assert bucket["stale_feed_blocks"] == 1
    assert bucket["repair_recurrence"] == {"missing-hash": 2}
    assert bucket["improvement_proposals"] == [
        {
            "failure_class": "missing-hash",
            "occurrences": 2,
            "state": "proposed",
            "existing_owner_cards": ["owner001"],
            "reason": None,
        }
    ]


def test_unhashed_evidence_and_missing_owner_fail_closed(tmp_path) -> None:
    """Unhashed inputs and absent owner cards remain unavailable."""
    store = CardStore(tmp_path)
    _attempt(store, "review01", "FAIL")
    store.append_event("review01", "verdict", "fixture", verdict="FAIL")
    _attempt(store, "review02", "FAIL")
    _finding(store, "finding1", "review02", "FAIL", failure_class="scope-drift")
    _attempt(store, "review03", "FAIL")
    _finding(store, "finding2", "review03", "FAIL", failure_class="scope-drift")

    bucket = project_from_card_store(tmp_path)["buckets"]["sk-m"]

    assert bucket["unavailable"]["unhashed_evidence"] == 1
    assert bucket["improvement_proposals"][0]["state"] == "unavailable"
    assert bucket["improvement_proposals"][0]["reason"] == "existing_owner_card_not_found"


def test_exclusions_and_output_are_host_and_provider_neutral(tmp_path) -> None:
    """The exact exclusions and host-neutral output contract are stable."""
    store = CardStore(tmp_path)
    for card_id in ("273845dc", "72c101a1", "89852b23"):
        _attempt(store, card_id, "PASS")
    _attempt(store, "review01", "PASS")

    report = project_from_card_store(tmp_path)
    serialized = str(report).lower()

    assert report["source"]["review_attempt_count"] == 1
    assert report["excluded_card_ids"] == ["273845dc", "72c101a1", "89852b23"]
    for forbidden in ("host", "node", "writer", "provider", "endpoint", "route"):
        assert forbidden not in serialized
