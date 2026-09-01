from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from skcapstone.mero_charter import observe, title_family


class Store:
    def __init__(self):
        self.cards = [
            SimpleNamespace(
                id="1", title="[SKL-X] legal", links={"verdict": "PASS", "evidence_sha256": "abc"}
            ),
            SimpleNamespace(id="2", title="[SKLEGAL-Y] liberty", links={}),
            SimpleNamespace(id="3", title="[REVIEW-X] review", links={}),
            SimpleNamespace(id="4", title="plain title", links={}),
            SimpleNamespace(id="5", title="[BROKEN title", links={}),
        ]

    def list_cards(self):
        return self.cards

    def _read_events(self, card_id):
        return {
            "1": [
                {"action": "claim", "ts": "2026-09-01T00:00:00+00:00"},
                {"action": "complete", "ts": "2026-08-31T00:00:00+00:00"},
            ]
        }.get(card_id, [])


def test_alias_classes_unprefixed_and_malformed_are_reported_read_only():
    store = Store()
    report = observe(store, now=datetime(2026, 9, 1, tzinfo=timezone.utc))

    stream = next(row for row in report["workstreams"] if row["workstream"] == "SKL")
    assert stream["card_count"] == 2
    assert stream["raw_families"] == ["SKL", "SKLEGAL"]
    assert stream["delivery_fraction_provisional"] == 0.5
    assert stream["claimed_last_7_days"] == 1
    assert report["excluded_class_tokens"][0] == {
        "token": "REVIEW",
        "count": 1,
        "reason": "Review is a card class and can apply to any project.",
    }
    assert report["unprefixed"]["count"] == 2
    assert {row["id"] for row in report["unprefixed"]["sample"]} == {"4", "5"}
    assert report["normalization"]["status"] == "DRAFT"
    assert report["normalization"]["ratified"] is False
    assert "48136bad" in report["delivery_status"]


def test_malformed_bracket_has_no_family():
    assert title_family("[BROKEN title") is None
