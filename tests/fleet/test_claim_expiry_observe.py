"""`observe()` against a REAL CardStore, never a hand-rolled event replay.

The first version of `observe` replayed the event log itself and was wrong
four separate times, each caught only by diffing against the fold on a live
store: a `(seq, ts)` sort key where events shard one file per writer and
`seq` restarts at 0 in each; `assign` ignored while `unassign` was honored;
a claim resurrecting a voided card; and `claim_revision` taken literally
where the fold falls back to `event_id`, plus a second claim by a different
owner winning where the fold refuses it.

So these tests drive the real `CardStore` and assert that `observe` agrees
with `fold()`. A test built from synthetic JSONL cannot establish that, and
that is exactly how four defects got through.
"""

from __future__ import annotations

import itertools
import json
import time
from datetime import datetime, timezone

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet.claim_expiry import evaluate, observe

HOUR = 3600.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _store(tmp_path):
    return CardStore(tmp_path)


_COUNTER = itertools.count(1)


def _card(store, title="a card", **kw):
    """Create a card with a real, non-path identifier.

    CardStore.create validates the id as a lock identifier, so an empty or
    path-like id is rejected outright.
    """
    card_id = kw.pop("id", None) or f"{next(_COUNTER):08x}"
    return store.create(CardCore(id=card_id, title=title, **kw))


def _held(tmp_path):
    return {o.card_id: o for o in observe(tmp_path)}


def test_an_unclaimed_card_is_not_reported(tmp_path):
    store = _store(tmp_path)
    _card(store, "never claimed")
    assert observe(tmp_path) == []


def test_a_claimed_card_is_reported_with_its_fold_revision(tmp_path):
    store = _store(tmp_path)
    cid = _card(store, "claimed")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="rev-1")
    got = _held(tmp_path)
    assert cid in got
    assert got[cid].owner == "w1"
    assert got[cid].claim_revision == "rev-1"


def test_a_claim_with_no_revision_still_gets_the_folds_event_id_fence(tmp_path):
    """The fold is `claim_revision or event_id`, so a fence ALWAYS exists.

    Reading it as `or None` made every such card permanently unreclaimable
    with reason "no-claim-revision". On one live store 38 of 106 held cards
    had no explicit claim_revision, so that reading disabled the mechanism
    for a third of its own population.
    """
    store = _store(tmp_path)
    cid = _card(store, "claim without an explicit revision")
    event = store.append_event(cid, "claim", "w2", owner="w2")
    got = _held(tmp_path)
    assert got[cid].claim_revision, "the fold supplies event_id as the fence"
    assert got[cid].claim_revision == event["event_id"]

    verdicts = evaluate(
        (
            [got[cid]._replace(last_owner_event_at=time.time() - 100 * HOUR)]
            if hasattr(got[cid], "_replace")
            else [got[cid]]
        ),
        now=time.time(),
        ttl_seconds=48 * HOUR,
    )
    assert verdicts[0].reason != "no-claim-revision"


def test_a_released_card_is_not_reported(tmp_path):
    store = _store(tmp_path)
    cid = _card(store, "released")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r1")
    store.append_event(
        cid, "release_claim", "mero", released_owner="w1", expected_claim_revision="r1"
    )
    assert cid not in _held(tmp_path)


def test_a_release_with_the_wrong_revision_does_not_free_the_card(tmp_path):
    """The fold applies a CAS fence on release. A replay that cleared
    ownership on ANY release_claim reported such a card free while the fold
    still held it, hiding it from the very mechanism meant to collect it."""
    store = _store(tmp_path)
    cid = _card(store, "release with a stale revision")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r1")
    store.append_event(
        cid, "release_claim", "someone", released_owner="w1", expected_claim_revision="STALE"
    )
    got = _held(tmp_path)
    assert cid in got, "the fold refuses a release fenced on the wrong revision"
    assert got[cid].owner == "w1"


def test_a_second_claim_by_a_different_owner_does_not_steal_the_card(tmp_path):
    """The fold refuses a concurrent claim and KEEPS the first owner.

    A replay that let the newer claim win named the losing identity as
    owner, so a card a live worker holds could surface in the would-reclaim
    list under a stale owner's name. That breaks the rollout's phase-2 gate.
    """
    store = _store(tmp_path)
    cid = _card(store, "contended")
    store.append_event(cid, "claim", "first", owner="first", claim_revision="r1")
    store.append_event(cid, "claim", "second", owner="second", claim_revision="r2")
    got = _held(tmp_path)
    assert got[cid].owner == "first", "the first claim holds until released"
    assert got[cid].claim_revision == "r1"


def test_a_re_claim_by_the_SAME_owner_is_accepted(tmp_path):
    """Complement: the refusal is about a DIFFERENT owner. Card f17d9e32 on
    chi re-claimed itself twice, and counting claims against releases to
    detect stuck cards overcounted because of exactly this."""
    store = _store(tmp_path)
    cid = _card(store, "self re-claim")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r1")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r2")
    got = _held(tmp_path)
    assert got[cid].owner == "w1"
    assert got[cid].claim_revision == "r2"


def test_a_voided_card_is_not_reported(tmp_path):
    """Cards 7e2c6788 and a80f87a9 on chi each took a claim AFTER being
    voided (by 21 seconds, and by an hour). Resurrecting them reintroduces
    the regression that left 88 of 114 voids silently ineffective.

    That shape cannot be built through the API any more: append_event now
    refuses with "void is a terminal decision", which is the write-time half
    of the guard. Those two cards predate it. So the historical shape is
    written straight to the shard, which is also the stronger test: it
    proves the READ path refuses to resurrect a card even when the bytes on
    disk say someone claimed it.
    """
    store = _store(tmp_path)
    cid = _card(store, "voided then claimed")
    store.append_event(cid, "void", "coord", reason="superseded")

    shard = tmp_path / "cards" / cid / "events" / "late@somehost.jsonl"
    shard.write_text(
        json.dumps(
            {
                "event_id": "deadbeef",
                "ts": _iso_now(),
                "writer": "late",
                "node": "somehost",
                "seq": 0,
                "action": "claim",
                "owner": "late",
                "claim_revision": "r9",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert cid not in _held(
        tmp_path
    ), "a voided card stays dead no matter what claim bytes follow it"


def test_idleness_is_measured_from_the_owners_last_event_not_the_claim(tmp_path):
    store = _store(tmp_path)
    cid = _card(store, "worked on")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r1")
    store.append_event(cid, "describe", "w1", text="progress")
    got = _held(tmp_path)
    assert got[cid].last_owner_event_at == pytest.approx(time.time(), abs=120)


def test_another_actors_events_do_not_refresh_the_owners_idleness(tmp_path):
    """A busy card is not a live worker. mero, jarvis and coord all write to
    cards they do not own, and counting their writes as owner activity would
    keep an abandoned claim alive forever."""
    store = _store(tmp_path)
    cid = _card(store, "busy but abandoned")
    store.append_event(cid, "claim", "w1", owner="w1", claim_revision="r1")
    owner_stamp = _held(tmp_path)[cid].last_owner_event_at
    for _ in range(5):
        store.append_event(cid, "mero_observation", "mero", note="still looking")
    after = _held(tmp_path)[cid].last_owner_event_at
    assert after == pytest.approx(
        owner_stamp, abs=2
    ), "another actor's events must not count as the owner touching the card"


def test_observe_on_a_missing_tree_returns_empty(tmp_path):
    assert observe(tmp_path / "nope") == []


def test_observe_agrees_with_the_fold_over_a_mixed_population(tmp_path):
    """The invariant that actually matters, asserted directly: for every
    card, observe reports it held exactly when the fold says it has an
    owner."""
    store = _store(tmp_path)
    ids = {}
    ids["plain"] = _card(store, "plain")
    ids["claimed"] = _card(store, "claimed")
    store.append_event(ids["claimed"], "claim", "w1", owner="w1", claim_revision="r1")
    ids["released"] = _card(store, "released")
    store.append_event(ids["released"], "claim", "w2", owner="w2", claim_revision="r2")
    store.append_event(
        ids["released"], "release_claim", "mero", released_owner="w2", expected_claim_revision="r2"
    )
    ids["voided"] = _card(store, "voided")
    store.append_event(ids["voided"], "claim", "w3", owner="w3", claim_revision="r3")
    store.append_event(ids["voided"], "void", "coord", reason="no")
    ids["contended"] = _card(store, "contended")
    store.append_event(ids["contended"], "claim", "a", owner="a", claim_revision="ra")
    store.append_event(ids["contended"], "claim", "b", owner="b", claim_revision="rb")

    fold_owners = {
        c.id: c.owner
        for c in store.list_cards(include_archived=False)
        if getattr(c, "owner", None) and not getattr(c, "archived", False)
    }
    replay_owners = {o.card_id: o.owner for o in observe(tmp_path)}
    assert replay_owners == fold_owners
