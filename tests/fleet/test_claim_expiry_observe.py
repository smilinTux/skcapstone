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


def test_observe_orders_by_timestamp_not_seq_across_writer_shards(tmp_path):
    """Regression: events are sharded one file per WRITER and `seq` restarts
    at 0 in every file, so seq is meaningless across writers.

    Card bf80259a on the live cluster is the worked example. The owning
    worker wrote claim(seq=0, 06:17:51) and claim(seq=1, 06:18:40) into its
    own shard; jarvis wrote release_claim(seq=0, 06:27:06) into a different
    shard. Ordering by seq first puts the release between the two claims, so
    the replay ends with the owner still set and the card is reported held
    forever, disagreeing with CardStore.fold(). Ordering by timestamp puts
    the release last, which is what actually happened.
    """
    d = tmp_path / "cards" / "bf80259a" / "events"
    d.mkdir(parents=True)
    (d / "pi-codex-chiap01-bf80259a@chiap01.jsonl").write_text(
        json.dumps(
            {
                "action": "claim",
                "owner": "pi-codex-chiap01-bf80259a",
                "writer": "pi-codex-chiap01-bf80259a",
                "claim_revision": "r1",
                "seq": 0,
                "ts": _iso(-120),
            }
        )
        + "\n"
        + json.dumps(
            {
                "action": "claim",
                "owner": "pi-codex-chiap01-bf80259a",
                "writer": "pi-codex-chiap01-bf80259a",
                "claim_revision": "r2",
                "seq": 1,
                "ts": _iso(-119),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (d / "jarvis@chiap03.jsonl").write_text(
        json.dumps(
            {
                "action": "release_claim",
                "released_owner": "pi-codex-chiap01-bf80259a",
                "writer": "jarvis",
                "expected_claim_revision": "r2",
                "seq": 0,
                "ts": _iso(-118),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert [
        o.card_id for o in observe(tmp_path)
    ] == [], "the release is the last event by timestamp, so nobody holds this card"


def test_observe_honors_assign_as_setting_an_owner(tmp_path):
    """Regression, card 122ebff1 on chi: 587 events, NOT ONE of them a claim.
    Its owner comes from a generic `assign`, and the fold reports `jarvis`.

    The first implementation honored `unassign` (clears) while ignoring
    `assign` (sets), which is asymmetric: it under-reported held cards, so
    the very claims the expiry path exists to collect were invisible to it.
    """
    _card(
        tmp_path,
        "122ebff1",
        [
            {"action": "move", "writer": "coord", "ts": _iso(-400)},
            {"action": "assign", "owner": "jarvis", "writer": "coord", "ts": _iso(-300)},
            {"action": "move", "writer": "coord", "ts": _iso(-299)},
        ],
    )
    got = {o.card_id: o for o in observe(tmp_path)}
    assert "122ebff1" in got, "an assigned card is held"
    assert got["122ebff1"].owner == "jarvis"
    # No CAS fence exists for an assignment, so it must not be reclaimable.
    assert got["122ebff1"].claim_revision is None


def test_an_assigned_card_is_visible_but_never_reclaimable(tmp_path):
    """Visibility and reclaimability are different things. An assignment has
    no claim_revision, so releasing it would be unfenced; refuse, but still
    report it so an operator can see the card is held."""
    import time as _time
    from skcapstone.fleet.claim_expiry import evaluate

    _card(
        tmp_path,
        "aaaa0001",
        [
            {"action": "assign", "owner": "jarvis", "writer": "coord", "ts": _iso(-500)},
        ],
    )
    verdicts = evaluate(observe(tmp_path), now=_time.time(), ttl_seconds=48 * 3600)
    assert len(verdicts) == 1
    assert verdicts[0].reclaimable is False
    assert verdicts[0].reason == "no-claim-revision"


def test_observe_reopens_a_card_assigned_after_a_terminal_event(tmp_path):
    """Regression, card cec6b1c0 on chi. Real sequence:

        claim(pi-skg-config) -> release_claim -> claim(pi)
          -> complete -> complete -> assign(pi)

    The fold reports owner `pi` and a non-terminal column. The first
    implementation broke out of the replay on the first terminal action, so
    the trailing assign was never seen and the card vanished from the census
    while still being held.
    """
    _card(
        tmp_path,
        "cec6b1c0",
        [
            {
                "action": "claim",
                "owner": "pi-skg-config-cec6b1c0",
                "writer": "pi-skg-config-cec6b1c0",
                "claim_revision": "r1",
                "ts": _iso(-600),
            },
            {
                "action": "release_claim",
                "released_owner": "pi-skg-config-cec6b1c0",
                "writer": "jarvis",
                "expected_claim_revision": "r1",
                "ts": _iso(-596),
            },
            {
                "action": "claim",
                "owner": "pi",
                "writer": "pi",
                "claim_revision": "r2",
                "ts": _iso(-590),
            },
            {"action": "complete", "writer": "pi", "ts": _iso(-589)},
            {"action": "complete", "writer": "pi", "ts": _iso(-589)},
            {"action": "assign", "owner": "pi", "writer": "coord", "ts": _iso(-580)},
        ],
    )
    got = {o.card_id: o for o in observe(tmp_path)}
    assert "cec6b1c0" in got, "the trailing assign reopened this card"
    assert got["cec6b1c0"].owner == "pi"


def test_a_terminal_event_with_nothing_after_it_stays_terminal(tmp_path):
    """The complement of the test above: not breaking on terminal must not
    turn every completed card back into a held one."""
    _card(
        tmp_path,
        "bbbb0002",
        [
            {
                "action": "claim",
                "owner": "w1",
                "writer": "w1",
                "claim_revision": "r1",
                "ts": _iso(-100),
            },
            {"action": "complete", "writer": "w1", "ts": _iso(-90)},
            {"action": "move", "writer": "coord", "ts": _iso(-80)},
        ],
    )
    assert [o.card_id for o in observe(tmp_path)] == []


def test_unassign_after_assign_clears_ownership(tmp_path):
    """Regression, card 72df1b66 on chi, which alternates both primitives."""
    _card(
        tmp_path,
        "72df1b66",
        [
            {
                "action": "claim",
                "owner": "jarvis",
                "writer": "jarvis",
                "claim_revision": "r1",
                "ts": _iso(-700),
            },
            {"action": "unassign", "writer": "coord", "ts": _iso(-699)},
            {
                "action": "assign",
                "owner": "pi-skl-gateway-72df1b66",
                "writer": "coord",
                "ts": _iso(-600),
            },
        ],
    )
    got = {o.card_id: o for o in observe(tmp_path)}
    assert got["72df1b66"].owner == "pi-skl-gateway-72df1b66"

    _card(
        tmp_path,
        "cccc0003",
        [
            {"action": "assign", "owner": "somebody", "writer": "coord", "ts": _iso(-500)},
            {"action": "unassign", "writer": "coord", "ts": _iso(-499)},
        ],
    )
    assert "cccc0003" not in {o.card_id for o in observe(tmp_path)}


def test_a_claim_after_a_void_does_not_resurrect_the_card(tmp_path):
    """Regression with real history behind it.

    Cards 7e2c6788 and a80f87a9 on chi each received a claim AFTER being
    voided and archived (21 seconds later, and an hour later). The fold
    reports neither as held, and resurrecting them is a known costly
    regression: 88 of 114 voids were once silently ineffective, leaving 88
    cards resurrectable and reversing decisions the operator had already
    made.

    An intermediate version of `observe` cleared the terminal flag on any
    acquire, which resurrected exactly these two. Void and archive are
    final; only `complete` is reopenable.
    """
    _card(
        tmp_path,
        "7e2c6788",
        [
            {"action": "void", "writer": "codex-a8100010-r2", "ts": _iso(-200)},
            {"action": "archive", "writer": "codex-a8100010-r2", "ts": _iso(-200)},
            {
                "action": "claim",
                "owner": "pi-codex-review-chiap03-7e2c6788",
                "writer": "pi-codex-review-chiap03-7e2c6788",
                "claim_revision": "57f694d100",
                "ts": _iso(-199),
            },
        ],
    )
    assert [
        o.card_id for o in observe(tmp_path)
    ] == [], "a voided card must stay dead no matter what claims follow it"


def test_archive_alone_is_also_final(tmp_path):
    _card(
        tmp_path,
        "dddd0004",
        [
            {"action": "archive", "writer": "coord", "ts": _iso(-100)},
            {"action": "assign", "owner": "jarvis", "writer": "coord", "ts": _iso(-90)},
        ],
    )
    assert [o.card_id for o in observe(tmp_path)] == []


def test_complete_is_still_reopenable_after_the_void_fix(tmp_path):
    """Guard the distinction: tightening void must not also freeze
    `complete`, or cec6b1c0 disappears from the census again."""
    _card(
        tmp_path,
        "eeee0005",
        [
            {
                "action": "claim",
                "owner": "pi",
                "writer": "pi",
                "claim_revision": "r1",
                "ts": _iso(-100),
            },
            {"action": "complete", "writer": "pi", "ts": _iso(-99)},
            {"action": "assign", "owner": "pi", "writer": "coord", "ts": _iso(-90)},
        ],
    )
    assert [o.owner for o in observe(tmp_path)] == ["pi"]
