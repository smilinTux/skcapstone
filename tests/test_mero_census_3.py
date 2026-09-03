"""Card 2516480b: the typed recommendation envelope and its serializer.

AC2 of the census: every finding pins card, revisions, generations, source
events, evidence hashes, risk class, consumer action, and stop conditions;
the emitted event round-trips through the serializer/parser pair.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone import mero_census as mc

from tests.census_support import _census, _home, _recs


@pytest.fixture()
def board(tmp_path: Path) -> CardStore:
    """The small shared board, built through the real serializer."""
    home = _home(tmp_path)
    store = CardStore(home)
    from tests.census_support import build_board

    store.create(CardCore(id="aaaa0001", title="dep", created_by="jarvis"))
    build_board(store)
    return store


# ---------------------------------------------------------------------------
# AC2: the typed recommendation envelope.
# ---------------------------------------------------------------------------


class TestRecommendationEnvelope:
    def test_every_finding_pins_the_required_fields(self, board: CardStore) -> None:
        report = _census(board.home).run()
        assert report.findings
        for finding in report.findings:
            assert finding["card_id"]
            assert finding["card_revision"]
            assert finding["blocker_generation"]
            assert finding["generation"]
            assert finding["source_events"], "findings must cite source events"
            for ref in finding["source_events"]:
                assert set(ref) >= {"event_id", "ts", "action", "writer"}
            assert finding["evidence_sha256"], "findings must pin evidence hashes"
            for digest in finding["evidence_sha256"]:
                assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
            assert finding["risk_class"] in {r.value for r in mc.RiskClass}
            assert finding["proposed_consumer_action"]
            assert finding["stop_conditions"], "findings must pin stop conditions"
            assert finding["recommendation_id"].startswith("mrc-")

    def test_emitted_event_is_typed_and_complete(self, board: CardStore) -> None:
        census = _census(board.home)
        report = census.run()
        census.emit(report)
        rows = _recs(board, "bbbb0002")
        assert rows
        row = rows[0]
        assert row["schema"] == mc.RECOMMENDATION_SCHEMA
        assert row["writer"] == "mero"
        assert row["observed_by"] == "mero"
        assert row["finding_type"] in {t.value for t in mc.CensusFindingType}
        # the durable line round-trips through the serializer/parser pair
        line = mc.recommendation_event_to_json(row)
        assert mc.parse_recommendation_line(line) == row


# ---------------------------------------------------------------------------
# AC5 support: serializer discipline.
# ---------------------------------------------------------------------------


class TestSerializerDiscipline:
    def test_event_to_json_then_parse_round_trips(self) -> None:
        event = {"b": 1, "a": [1, 2, {"c": "x"}], "schema": mc.RECOMMENDATION_SCHEMA}
        line = mc.recommendation_event_to_json(event)
        assert line == json.dumps(event, sort_keys=True, separators=(",", ":"))
        assert mc.parse_recommendation_line(line) == event

    def test_parse_rejects_non_json_garbage(self) -> None:
        with pytest.raises(ValueError):
            mc.parse_recommendation_line("this is not json")

    def test_parse_rejects_non_object_lines(self) -> None:
        with pytest.raises(ValueError):
            mc.parse_recommendation_line("[1, 2, 3]")

    def test_findings_digests_are_stable(self, board: CardStore) -> None:
        first = _census(board.home).run()
        second = _census(board.home).run()
        ids1 = {f["recommendation_id"] for f in first.findings}
        ids2 = {f["recommendation_id"] for f in second.findings}
        assert ids1 == ids2
