"""Retained exits that no acceptance step can act on are classified, never accepted."""

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet.production_review_evidence import ReviewEvidenceError
from skcapstone.fleet.production_review_finish import once

CARD = "0a1b2c3d"


class _Row:
    def __init__(self, labels, status="review"):
        self.labels = labels
        self.status = type("S", (), {"value": status})()


class _Store:
    def __init__(self, home, rows):
        self.rows = rows

    def fold(self, card):
        return self.rows.get(card)


@pytest.fixture
def directory(tmp_path):
    return tmp_path / "pair"


def _done(monkeypatch, source="done"):
    rows = {"99887766": _Row(["source-only"], source), CARD: _Row(["source-only"], "done")}
    monkeypatch.setattr(acceptance, "CardStore", lambda home: _Store(home, rows))


def _finish(directory, context, **override):
    once(directory / "context.json", context)
    result = {
        "schema": "skfleet.source-review-acceptance/v1",
        "accepted": True,
        "context_sha256": acceptance.digest(context),
        "source_card": "99887766",
        "review_card": CARD,
        **override,
    }
    once(directory / "finished.json", result)
    return result


def test_finished_pair_is_accepted_without_replaying_live_policy(tmp_path, directory, monkeypatch):
    result = _finish(directory, {"source": {"card": "99887766"}})
    _done(monkeypatch)
    assert acceptance._retired_exit(tmp_path, CARD, directory) == {
        "card": CARD,
        "state": "accepted",
        "receipt": result,
        "historical": True,
    }


@pytest.mark.parametrize(
    "override",
    [
        {"accepted": False},
        {"context_sha256": "0" * 64},
        {"review_card": "ffffffff"},
        {"schema": "other"},
    ],
)
def test_finished_record_not_bound_to_context_is_refused(
    tmp_path, directory, monkeypatch, override
):
    _finish(directory, {"source": {"card": "99887766"}}, **override)
    _done(monkeypatch)
    with pytest.raises(ReviewEvidenceError, match="differs from context"):
        acceptance._retired_exit(tmp_path, CARD, directory)


def test_reopened_accepted_pair_is_refused(tmp_path, directory, monkeypatch):
    _finish(directory, {"source": {"card": "99887766"}})
    _done(monkeypatch, source="review")
    with pytest.raises(ReviewEvidenceError, match="no longer done"):
        acceptance._retired_exit(tmp_path, CARD, directory)


@pytest.mark.parametrize("row", [None, _Row(["review", "seat-seraph"])])
def test_exit_of_non_source_only_review_is_not_applicable(tmp_path, directory, monkeypatch, row):
    monkeypatch.setattr(acceptance, "CardStore", lambda home: _Store(home, {CARD: row}))
    assert acceptance._retired_exit(tmp_path, CARD, directory)["state"] == "not-applicable"


def test_source_only_review_without_context_still_collects(tmp_path, directory, monkeypatch):
    rows = {CARD: _Row(["review", "seat-seraph", "source-only"])}
    monkeypatch.setattr(acceptance, "CardStore", lambda home: _Store(home, rows))
    assert acceptance._retired_exit(tmp_path, CARD, directory) is None


def test_collected_context_is_never_retired_by_label(tmp_path, directory, monkeypatch):
    once(directory / "context.json", {})
    monkeypatch.setattr(acceptance, "CardStore", lambda home: _Store(home, {CARD: None}))
    assert acceptance._retired_exit(tmp_path, CARD, directory) is None
