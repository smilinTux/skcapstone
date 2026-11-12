from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.link_cycle import _digest
from skcapstone.link_observation_feed import ObservationFeedError, load_observation_feed
from skcapstone.seat_cycle_entrypoint import link_operation

HEAD = "a" * 40
BASE = "b" * 40
CARD_REVISION = "c" * 64


def payload(*, observed_at: str | None = None) -> dict:
    return {
        "schema": "skfleet.link-observation-feed/v1",
        "source_revision": "observer-rev-17",
        "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
        "producer": {
            "identity": "mediated-observer",
            "host": "chiap08",
            "session": "observer-session",
            "workspace": "observer-workspace",
        },
        "records": [
            {
                "observation": {
                    "repository": "smilinTux/skcapstone",
                    "number": 17,
                    "title": "bounded change",
                    "author": "contributor",
                    "head_sha": HEAD,
                    "base_sha": BASE,
                    "ci_state": "success",
                    "conflict_state": "clean",
                    "review_requests": [],
                    "source_card": "card-17",
                    "card_generation": "generation-1",
                    "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
                },
                "review_card_id": "review-17",
                "review_card_revision": CARD_REVISION,
            }
        ],
        "reviewer_candidates": [
            {
                "schema": "skfleet.reviewer-identity/v1",
                "name": "Seraph",
                "seat": "seraph",
                "identity": "seraph",
                "host": "chiap04",
                "session": "seraph-session",
                "workspace": "seraph-workspace",
                "fingerprint": "d" * 64,
            }
        ],
    }


def write_feed(path: Path, value: dict) -> None:
    canonical = {
        "source_revision": value["source_revision"],
        "observed_at": value["observed_at"],
        "producer": value["producer"],
        "records": value["records"],
        "reviewer_candidates": value["reviewer_candidates"],
    }
    value["evidence_sha256"] = _digest(canonical)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_valid_feed_drives_advisory_handoff(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    write_feed(feed_path, payload())
    result = link_operation(tmp_path, feed_path)
    assert result["cards_examined"] == 1
    assert result["recommendations"] == 1
    handoff = (tmp_path / "coordination/seat-cycles/link.handoffs.jsonl").read_text()
    assert "skfleet.pr-review-handoff/v1" in handoff
    assert "observer-rev-17" in handoff
    assert "c" * 64 in handoff


def test_replayed_evidence_is_rejected(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    value = payload()
    write_feed(feed_path, value)
    supplied = value["evidence_sha256"]
    with pytest.raises(ObservationFeedError, match="observation_feed_replayed"):
        load_observation_feed(feed_path, prior_evidence_sha256=supplied)


def test_different_prior_evidence_is_not_a_replay(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    write_feed(feed_path, payload())
    assert load_observation_feed(feed_path, prior_evidence_sha256="f" * 64).records
    assert load_observation_feed(feed_path, prior_evidence_sha256=None).records


def test_unchanged_feed_is_not_consumed_twice_by_link(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    write_feed(feed_path, payload())
    first = link_operation(tmp_path, feed_path)
    assert first["recommendations"] == 1
    handoffs = tmp_path / "coordination/seat-cycles/link.handoffs.jsonl"
    before = handoffs.read_text()
    second = link_operation(tmp_path, feed_path)
    assert second["reason"] == "observation_feed_replayed"
    assert handoffs.read_text() == before


def test_fresh_feed_after_a_consumed_one_is_accepted(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    write_feed(feed_path, payload())
    link_operation(tmp_path, feed_path)
    newer = payload(observed_at=(datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
    write_feed(feed_path, newer)
    again = link_operation(tmp_path, feed_path)
    assert again["recommendations"] == 1
    assert "reason" not in again


def test_rejected_feed_does_not_advance_the_replay_marker(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    write_feed(feed_path, payload())
    link_operation(tmp_path, feed_path)
    marker = tmp_path / "coordination/seat-cycles/link.last-feed-sha256"
    consumed = marker.read_text()
    feed_path.write_text("{not-json", encoding="utf-8")
    result = link_operation(tmp_path, feed_path)
    assert result["reason"] == "observation_feed_unreadable"
    assert marker.read_text() == consumed


def test_malformed_json_is_fail_closed(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    feed_path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ObservationFeedError, match="observation_feed_unreadable"):
        load_observation_feed(feed_path)


def test_jarvis_reviewer_candidate_is_rejected(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    value = payload()
    value["reviewer_candidates"][0]["name"] = "Jarvis"
    value["reviewer_candidates"][0]["seat"] = "jarvis"
    write_feed(feed_path, value)
    with pytest.raises(ObservationFeedError, match="seat is not eligible"):
        load_observation_feed(feed_path)


def test_jarvis_name_spoof_with_seraph_seat_is_rejected(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    value = payload()
    value["reviewer_candidates"][0]["name"] = "Jarvis"
    write_feed(feed_path, value)
    with pytest.raises(ObservationFeedError, match="name is not eligible"):
        load_observation_feed(feed_path)


def test_null_reviewer_identity_field_is_rejected(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    value = payload()
    value["reviewer_candidates"][0]["workspace"] = None
    write_feed(feed_path, value)
    with pytest.raises(ObservationFeedError, match="fields are incomplete"):
        load_observation_feed(feed_path)


def test_non_boolean_reviewer_eligibility_is_rejected(tmp_path: Path) -> None:
    feed_path = tmp_path / "link-observations.json"
    value = payload()
    value["reviewer_candidates"][0]["eligible"] = "false"
    write_feed(feed_path, value)
    with pytest.raises(ObservationFeedError, match="eligibility is invalid"):
        load_observation_feed(feed_path)


def test_missing_feed_is_bounded_noop(tmp_path: Path) -> None:
    result = link_operation(tmp_path, tmp_path / "missing.json")
    assert result == {"reason": "observation_feed_missing", "suppressed": 1}


def test_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "feed.json"
    value = payload()
    write_feed(path, value)
    value["records"][0]["observation"]["title"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ObservationFeedError, match="evidence_mismatch"):
        load_observation_feed(path)


def test_stale_feed_is_rejected(tmp_path: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    path = tmp_path / "feed.json"
    write_feed(path, payload(observed_at=old))
    with pytest.raises(ObservationFeedError, match="stale"):
        load_observation_feed(path)


def test_stale_inner_observation_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "feed.json"
    value = payload()
    value["records"][0]["observation"]["observed_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=16)
    ).isoformat()
    write_feed(path, value)
    with pytest.raises(ObservationFeedError, match="observation_stale"):
        load_observation_feed(path)
