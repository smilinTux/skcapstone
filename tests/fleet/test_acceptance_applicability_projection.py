from pathlib import Path

from skcapstone.fleet import production_acceptance as acceptance


class DuplicateProjectionStore:
    def __init__(self, current, legacy):
        self.current = current
        self.legacy = legacy

    def _read_events(self, _card):
        return self.current

    def _legacy_events(self, _card):
        return self.legacy


def _receipt(event_id, ts, value):
    return {
        "action": "link",
        "link_key": "applicability_receipt",
        "event_id": event_id,
        "ts": ts,
        "writer": "pi-seraph-chiap03-056a4633",
        "seq": 5,
        "link_value": value,
    }


def test_identical_legacy_projection_does_not_report_duplicate_receipts(monkeypatch):
    value = '{"schema":"skcapstone.applicability/v1"}'
    current = _receipt("event-1", "2026-10-10T21:13:32Z", value)
    legacy = {key: item for key, item in current.items() if key != "event_id"}
    store = DuplicateProjectionStore([current], [legacy])
    monkeypatch.setattr(acceptance, "_source_only_applicability", lambda *_: False)

    result = acceptance._source_applicability_error(store, "056a4633", Path("/unused"))

    assert result == "source-only applicability receipt differs"


def test_distinct_applicability_receipts_remain_a_refusal(monkeypatch):
    first = _receipt("event-1", "2026-10-10T21:13:32Z", '{"receipt":1}')
    second = _receipt("event-2", "2026-10-10T21:14:32Z", '{"receipt":2}')
    store = DuplicateProjectionStore([first, second], [])
    monkeypatch.setattr(acceptance, "_source_only_applicability", lambda *_: False)

    result = acceptance._source_applicability_error(store, "056a4633", Path("/unused"))

    assert result == "duplicate source-only applicability receipts"
