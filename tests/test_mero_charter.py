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


NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _store(cards, events=None):
    class S:
        def list_cards(self):
            return cards

        def _read_events(self, card_id):
            return (events or {}).get(card_id, [])

    return S()


def _card(card_id, title, links=None):
    return SimpleNamespace(id=card_id, title=title, links=links or {})


def test_busy_stream_without_delivery_is_the_surfaced_pathology():
    cards = [_card("1", "[SKGW-A] gateway"), _card("2", "[SKGW-B] gateway")]
    events = {"1": [{"action": "claim", "ts": "2026-08-30T00:00:00Z"}]}
    report = observe(_store(cards, events), now=NOW)
    assert [row["workstream"] for row in report["pathologies"]] == ["SKGW"]
    assert report["pathologies"][0]["most_recent_delivered_card"] is None


def test_delivery_requires_verdict_and_evidence_and_a_terminal_done_event():
    done = [{"action": "complete", "ts": "2026-08-31T00:00:00+00:00"}]
    cases = {
        "ok": ({"verdict": "PASS", "evidence": "x"}, done, 1),
        "no-evidence": ({"verdict": "PASS"}, done, 0),
        "no-verdict": ({"evidence_sha256": "x"}, done, 0),
        "reopened": (
            {"verdict": "PASS", "evidence": "x"},
            done + [{"action": "reopen", "ts": "2026-08-31T01:00:00+00:00"}],
            0,
        ),
    }
    for name, (links, evs, delivered) in cases.items():
        report = observe(_store([_card("1", "[FLEET-X] t", links)], {"1": evs}), now=NOW)
        row = report["workstreams"][0]
        assert row["delivery_fraction_provisional"] == delivered, name


def test_event_timestamps_are_compared_as_instants_not_strings():
    links = {"verdict": "PASS", "evidence": "x"}
    cards = [_card("1", "[FLEET-A] a", links), _card("2", "[FLEET-B] b", links)]
    events = {
        # 23:00-05:00 is 04:00Z the next day, later than 01:00Z, though the string sorts earlier.
        "1": [{"action": "complete", "ts": "2026-08-20T23:00:00-05:00"}],
        "2": [{"action": "complete", "ts": "2026-08-21T01:00:00+00:00"}],
    }
    row = observe(_store(cards, events), now=NOW)["workstreams"][0]
    assert row["most_recent_delivered_card"] == "2026-08-21T04:00:00+00:00"


def test_malformed_and_naive_event_timestamps_are_ignored_not_fatal():
    cards = [_card("1", "[FLEET-A] a", {"verdict": "PASS", "evidence": "x"})]
    events = {
        "1": [
            {"action": "complete", "ts": "yesterday"},
            {"action": "complete", "ts": "2026-08-31T00:00:00"},
            {"action": "complete"},
        ]
    }
    row = observe(_store(cards, events), now=NOW)["workstreams"][0]
    assert row["delivery_fraction_provisional"] == 0


def test_class_tokens_never_become_workstreams():
    cards = [_card("1", "[REVIEW-x] r"), _card("2", "[EVIDENCE-y] e")]
    report = observe(_store(cards), now=NOW)
    assert report["workstreams"] == []
    counts = {row["token"]: row["count"] for row in report["excluded_class_tokens"]}
    assert counts["REVIEW"] == 1 and counts["EVIDENCE"] == 1


def test_empty_board_reports_zero_scatter():
    assert observe(_store([]), now=NOW)["unprefixed"]["fraction"] == 0.0


def test_packaged_alias_data_is_draft_and_every_entry_has_a_reason():
    from skcapstone.mero_charter import load_normalization

    data = load_normalization()
    assert data["status"] == "DRAFT" and data["ratified"] is False
    assert data["aliases"] and data["class_tokens"]
    assert all(row["reason"].strip() and row["workstream"] for row in data["aliases"].values())
    assert all(reason.strip() for reason in data["class_tokens"].values())


def test_cli_observes_a_real_cardstore_and_writes_nothing(tmp_path):
    import json

    from click.testing import CliRunner
    from skcoord.card_store import CardCore, CardStore

    from skcapstone.cli import main

    store = CardStore(tmp_path)
    for index, title in enumerate(["[SKLEGAL-1] a", "[SKL-2] b", "[REVIEW-3] c", "plain"]):
        store.create(
            CardCore(
                id=f"{index + 1:08x}",
                title=title,
                initial_owner="fixture",
                initial_claim_revision="a" * 32,
            )
        )
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    result = CliRunner().invoke(main, ["mero", "charter", "observe", "--home", str(tmp_path)])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    skl = next(row for row in report["workstreams"] if row["workstream"] == "SKL")
    assert skl["card_count"] == 2 and skl["raw_families"] == ["SKL", "SKLEGAL"]
    assert report["unprefixed"]["count"] == 1
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
