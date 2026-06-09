"""Dreaming engine routes its output to GTD someday-maybe, not the actionable inbox."""
import json
from datetime import datetime, timezone
from pathlib import Path

from skcapstone.dreaming import DreamingEngine, DreamResult


def test_dream_output_goes_to_someday_not_inbox(tmp_path: Path):
    eng = DreamingEngine(home=tmp_path)
    result = DreamResult(
        dreamed_at=datetime(2026, 6, 8, tzinfo=timezone.utc),
        insights=["i1", "i2"],
        connections=["c1"],
        questions=["q1"],
    )
    eng._capture_to_gtd_someday(result)

    gtd = tmp_path / "coordination" / "gtd"
    someday = json.loads((gtd / "someday-maybe.json").read_text())
    assert len(someday) == 4
    assert all(
        it["status"] == "someday" and it["source"] == "dreaming-engine"
        for it in someday
    )
    # The actionable inbox must NOT be polluted.
    assert not (gtd / "inbox.json").exists()


def test_no_items_writes_nothing(tmp_path: Path):
    eng = DreamingEngine(home=tmp_path)
    eng._capture_to_gtd_someday(
        DreamResult(dreamed_at=datetime(2026, 6, 8, tzinfo=timezone.utc))
    )
    assert not (tmp_path / "coordination" / "gtd" / "someday-maybe.json").exists()
