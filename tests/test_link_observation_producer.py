from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.link_observation_feed import load_observation_feed
from skcapstone.link_observation_producer import (
    ProducerError,
    ProducerIdentity,
    _mapping_evidence,
    build_feed,
    produce,
)

HEAD = "a" * 40
BASE = "b" * 40
REVISION = "c" * 64


class FakeConnector:
    def __init__(self, rows=None, error: str | None = None):
        self.rows = rows or []
        self.error = error

    def list_open(self, repository: str):
        if self.error:
            raise ProducerError(self.error)
        return self.rows


def producer() -> ProducerIdentity:
    return ProducerIdentity("link-producer", "chiap08", "session-1", "workspace-1")


def row(number: int = 17, *, snapshot_at: str | None = None, failed: bool = False) -> dict:
    return {
        "number": number,
        "title": "bounded change",
        "author": {"login": "contributor"},
        "headRefOid": HEAD,
        "baseRefOid": BASE,
        "mergeable": True,
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"conclusion": "FAILURE" if failed else "SUCCESS"}],
        "reviewRequests": [],
        "snapshot_at": snapshot_at or datetime.now(timezone.utc).isoformat(),
    }


def lineage(*, complete: bool = True) -> dict:
    record = {
        "source_card": "card-17",
        "card_generation": "generation-1",
        "review_card_id": "review-17",
        "review_card_revision": REVISION,
        "review_verdict": "PASS",
        "head_revision": HEAD,
        "base_revision": BASE,
        "mapping_evidence_sha256": _mapping_evidence(
            "smilinTux/skcapstone",
            17,
            HEAD,
            BASE,
            "card-17",
            "generation-1",
            "review-17",
            REVISION,
            "PASS",
        ),
    }
    if not complete:
        record.pop("review_card_id")
    return {
        "schema": "skfleet.link-lineage/v1",
        "coverage": {"unresolved": 0, "lineage-complete": 1, "excluded": 0},
        "records": {"smilinTux/skcapstone#17": record},
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


def write_lineage(path: Path, value: dict | None = None) -> None:
    path.write_text(json.dumps(value or lineage()), encoding="utf-8")


def test_clean_and_failed_prs_produce_typed_feed() -> None:
    clean = FakeConnector([row()])
    payload, result = build_feed(
        clean,
        repositories=["smilinTux/skcapstone"],
        lineage=lineage(),
        producer=producer(),
    )
    assert result.healthy is True
    assert payload is not None
    assert payload["records"][0]["observation"]["ci_state"] == "success"
    failed_payload, _ = build_feed(
        FakeConnector([row(failed=True)]),
        repositories=["smilinTux/skcapstone"],
        lineage=lineage(),
        producer=producer(),
    )
    assert failed_payload["records"][0]["observation"]["ci_state"] == "failure"


def test_github_rest_nested_head_and_base_shas_are_supported() -> None:
    value = row()
    value.pop("headRefOid")
    value.pop("baseRefOid")
    value["head"] = {"sha": HEAD}
    value["base"] = {"sha": BASE}
    payload, result = build_feed(
        FakeConnector([value]),
        repositories=["smilinTux/skcapstone"],
        lineage=lineage(),
        producer=producer(),
    )
    assert result.healthy is True
    assert payload["records"][0]["observation"]["head_sha"] == HEAD
    assert payload["records"][0]["observation"]["base_sha"] == BASE


def test_global_unresolved_count_does_not_hide_a_complete_record(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    output = tmp_path / "link-observations.json"
    value = lineage()
    value["coverage"]["unresolved"] = 1
    write_lineage(lineage_path, value)
    output.write_text("last-valid-feed\n", encoding="utf-8")
    result = produce(
        connector=FakeConnector([row()]),
        repositories=["smilinTux/skcapstone"],
        lineage_path=lineage_path,
        output_path=output,
        producer=producer(),
    )
    assert result.healthy is True
    assert result.reason == "complete"
    payload = json.loads(output.read_text())
    assert payload["lineage_status"]["complete"] == 1
    assert payload["lineage_status"]["unresolved"] == 0


def test_orphaned_pr_is_blocked(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    write_lineage(lineage_path, {**lineage(), "records": {}})
    result = produce(
        connector=FakeConnector([row()]),
        repositories=["smilinTux/skcapstone"],
        lineage_path=lineage_path,
        output_path=tmp_path / "feed.json",
        producer=producer(),
    )
    assert result.healthy is False
    assert result.reason == "lineage_incomplete"


def test_mixed_lineage_publishes_complete_subset_and_reports_omission() -> None:
    second = row()
    second["number"] = 43
    second["headRefOid"] = "c" * 40
    payload, result = build_feed(
        FakeConnector([row(), second]),
        repositories=["smilinTux/skcapstone"],
        lineage=lineage(),
        producer=producer(),
    )
    assert result.healthy is True
    assert result.reason == "complete_with_unresolved"
    assert len(payload["records"]) == 1
    assert payload["lineage_status"]["complete"] == 1
    assert payload["lineage_status"]["unresolved"] == 1


def test_stale_snapshot_connector_data_is_rejected(tmp_path: Path) -> None:
    old = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat()
    lineage_path = tmp_path / "lineage.json"
    write_lineage(lineage_path)
    with pytest.raises(ProducerError, match="snapshot_stale"):
        produce(
            connector=FakeConnector([row(snapshot_at=old)]),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=tmp_path / "feed.json",
            producer=producer(),
        )


def test_duplicate_and_malformed_prs_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ProducerError, match="duplicate_pr"):
        build_feed(
            FakeConnector([row(), row()]),
            repositories=["smilinTux/skcapstone"],
            lineage=lineage(),
            producer=producer(),
        )
    malformed = row()
    malformed["headRefOid"] = "not-a-sha"
    with pytest.raises(ProducerError, match="pr_sha_malformed"):
        build_feed(
            FakeConnector([malformed]),
            repositories=["smilinTux/skcapstone"],
            lineage=lineage(),
            producer=producer(),
        )


def test_connector_failure_preserves_existing_output(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    write_lineage(lineage_path)
    output = tmp_path / "feed.json"
    output.write_text("last-valid-feed\n", encoding="utf-8")
    with pytest.raises(ProducerError, match="connector_unavailable"):
        produce(
            connector=FakeConnector(error="connector_unavailable"),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=output,
            producer=producer(),
        )
    assert output.read_text() == "last-valid-feed\n"


def test_complete_output_is_atomic_and_loadable(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    write_lineage(lineage_path)
    output = tmp_path / "feed.json"
    result = produce(
        connector=FakeConnector([row()]),
        repositories=["smilinTux/skcapstone"],
        lineage_path=lineage_path,
        output_path=output,
        producer=producer(),
    )
    assert result.healthy is True
    loaded = load_observation_feed(output)
    assert loaded.source_revision == result.source_revision
    assert loaded.evidence_sha256 == result.evidence_sha256


def test_invalid_reviewer_candidate_is_blocked_before_feed_emission(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    invalid = lineage()
    invalid["reviewer_candidates"][0]["seat"] = "jarvis"
    write_lineage(lineage_path, invalid)
    with pytest.raises(ProducerError, match="lineage_reviewer_invalid"):
        produce(
            connector=FakeConnector([row()]),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=tmp_path / "feed.json",
            producer=producer(),
        )


def test_jarvis_name_spoof_with_seraph_seat_is_blocked_before_feed_emission(
    tmp_path: Path,
) -> None:
    lineage_path = tmp_path / "lineage.json"
    invalid = lineage()
    invalid["reviewer_candidates"][0]["name"] = "Jarvis"
    write_lineage(lineage_path, invalid)
    with pytest.raises(ProducerError, match="lineage_reviewer_invalid"):
        produce(
            connector=FakeConnector([row()]),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=tmp_path / "feed.json",
            producer=producer(),
        )


def test_null_reviewer_identity_field_is_blocked_before_feed_emission(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    invalid = lineage()
    invalid["reviewer_candidates"][0]["workspace"] = None
    write_lineage(lineage_path, invalid)
    with pytest.raises(ProducerError, match="lineage_reviewer_invalid"):
        produce(
            connector=FakeConnector([row()]),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=tmp_path / "feed.json",
            producer=producer(),
        )


def test_non_boolean_reviewer_eligibility_is_blocked_before_feed_emission(tmp_path: Path) -> None:
    lineage_path = tmp_path / "lineage.json"
    invalid = lineage()
    invalid["reviewer_candidates"][0]["eligible"] = "false"
    write_lineage(lineage_path, invalid)
    with pytest.raises(ProducerError, match="lineage_reviewer_invalid"):
        produce(
            connector=FakeConnector([row()]),
            repositories=["smilinTux/skcapstone"],
            lineage_path=lineage_path,
            output_path=tmp_path / "feed.json",
            producer=producer(),
        )
