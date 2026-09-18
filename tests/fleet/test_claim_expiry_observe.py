import json
import time
from pathlib import Path

import pytest

from skcapstone.fleet.claim_expiry import observe


def _card(home: Path, cid: str, events: list[dict]) -> None:
    d = home / "cards" / cid / "events"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "0001.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _iso(offset_h: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=offset_h)).isoformat()


def test_observe_reports_held_claim_with_owner_activity(tmp_path):
    _card(
        tmp_path,
        "aaaa1111",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
            {"action": "move", "writer": "jarvis", "ts": _iso(-60)},
        ],
    )
    got = {o.card_id: o for o in observe(tmp_path)}
    assert "aaaa1111" in got
    o = got["aaaa1111"]
    assert o.owner == "jarvis"
    assert o.claim_revision == "rev1"
    # last OWNER event is the move at -60h, not the claim at -100h
    assert o.last_owner_event_at == pytest.approx(time.time() - 60 * 3600, abs=120)


def test_observe_ignores_a_released_claim(tmp_path):
    _card(
        tmp_path,
        "bbbb2222",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
            {
                "action": "release_claim",
                "released_owner": "jarvis",
                "writer": "mero",
                "expected_claim_revision": "rev1",
                "ts": _iso(-90),
            },
        ],
    )
    assert [o.card_id for o in observe(tmp_path)] == []


def test_observe_ignores_a_completed_card(tmp_path):
    _card(
        tmp_path,
        "cccc3333",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "rev1",
                "ts": _iso(-100),
            },
            {"action": "complete", "writer": "jarvis", "ts": _iso(-90)},
        ],
    )
    assert [o.card_id for o in observe(tmp_path)] == []


def test_observe_uses_the_latest_claim_generation(tmp_path):
    """A re-claim by the same owner supersedes the first. The revision
    reported must be the newest, or the CAS fence targets a dead generation."""
    _card(
        tmp_path,
        "dddd4444",
        [
            {
                "action": "claim",
                "owner": "w1",
                "writer": "w1",
                "claim_revision": "old",
                "ts": _iso(-100),
            },
            {
                "action": "claim",
                "owner": "w1",
                "writer": "w1",
                "claim_revision": "new",
                "ts": _iso(-99),
            },
        ],
    )
    o = observe(tmp_path)[0]
    assert o.claim_revision == "new"


def test_observe_survives_a_corrupt_line(tmp_path):
    d = tmp_path / "cards" / "eeee5555" / "events"
    d.mkdir(parents=True)
    (d / "0001.jsonl").write_text(
        "not json\n"
        + json.dumps(
            {
                "action": "claim",
                "owner": "w2",
                "writer": "w2",
                "claim_revision": "r",
                "ts": _iso(-100),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert observe(tmp_path)[0].owner == "w2"


def test_observe_on_missing_tree_returns_empty(tmp_path):
    assert observe(tmp_path / "nope") == []
