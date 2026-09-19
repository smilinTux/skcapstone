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


# ---------------------------------------------------------------------------
# The evidence store. `coord link`, `verdict` and `evidence` are written to
# ~/.skcapstone/coordination/card_events/*.jsonl through CardEventLog and
# NEVER touch cards/<id>/events/. Measured on chi that store holds 81,207
# records against roughly 13,000 in the per-card shards, so an idle clock
# that reads only the shards is blind to the entire review workflow.
#
# An adversarial review demonstrated the consequence end to end: a worker
# claimed at T-50h, posted evidence hourly and a verdict seconds before the
# sweep through the real write paths, and still measured as 50 hours idle.
# The card folded to owner=None while the worker was alive and working.
# ---------------------------------------------------------------------------


def _evidence(home, card_id, writer, *, action="link", hours_ago=0.0):
    """Append one record to the evidence store, in its real on-disk shape."""
    from datetime import datetime, timedelta, timezone

    d = home / "coordination" / "card_events"
    d.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()
    with (d / "chiap01.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "card_id": card_id,
                    "action": action,
                    "writer": writer,
                    "ts": stamp,
                    "link_key": "evidence",
                    "link_value": "/tmp/x",
                }
            )
            + "\n"
        )


def _age_shards(home, card_id, hours):
    """Backdate every event in the per-card shards, to isolate the overlay."""
    from datetime import datetime, timedelta, timezone

    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    for shard in (home / "cards" / card_id / "events").glob("*.jsonl"):
        out = []
        for line in shard.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            event["ts"] = stamp
            out.append(json.dumps(event))
        shard.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_evidence_store_activity_keeps_a_live_workers_claim_alive(tmp_path):
    """The attack, defended. Without the overlay this card is 50h idle and
    reclaimable while its worker is posting verdicts."""
    store = _store(tmp_path)
    cid = _card(store, "worker posting evidence")
    store.append_event(
        cid, "claim", "pi-codex-chiap01-w", owner="pi-codex-chiap01-w", claim_revision="r1"
    )
    _age_shards(tmp_path, cid, 50)
    _evidence(tmp_path, cid, "pi-codex-chiap01-w", action="verdict", hours_ago=0.01)

    got = _held(tmp_path)[cid]
    assert got.last_owner_event_at > 0
    verdict = evaluate([got], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert (
        verdict.reclaimable is False
    ), "a worker posting verdicts is alive; the overlay must see it"
    assert verdict.idle_seconds < HOUR


def test_a_seat_writing_evidence_does_NOT_keep_a_dead_claim_alive(tmp_path):
    """The complement, and the reason the overlay is not simply "any event".

    Measured on chi, three held cards had an owner idle 32 to 34 hours while
    a seat wrote to the card within 10 minutes. Counting that as liveness
    would keep dead claims alive forever, which is the bug being fixed.

    What excludes them is the card-id rule, not a list of seat names: a card
    id is eight hex characters and no seat name contains one. A blocklist
    was written first and removed after a mutation check showed it never
    fired.
    """
    store = _store(tmp_path)
    cid = _card(store, "abandoned but observed")
    store.append_event(cid, "claim", "dead-worker", owner="dead-worker", claim_revision="r1")
    _age_shards(tmp_path, cid, 200)
    for seat in ("mero", "jarvis", "lumina", "coord"):
        _evidence(tmp_path, cid, seat, action="link", hours_ago=0.01)

    got = _held(tmp_path)[cid]
    verdict = evaluate([got], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert verdict.reclaimable is True, "a seat's observations are not the holder being alive"
    assert verdict.idle_seconds > 190 * HOUR


def test_a_sibling_worker_identity_for_the_same_card_counts_as_activity(tmp_path):
    """Card 0f7b2e6c on chi: the owner `pi-codex-chiap02-0f7b2e6c` wrote one
    event while its sibling `pi-codex-chiap08-0f7b2e6c` wrote four. The
    owning identity is frequently not the working identity."""
    store = _store(tmp_path)
    cid = _card(store, "worked by a sibling", id="0f7b2e6c")
    store.append_event(
        cid,
        "claim",
        f"pi-codex-chiap02-{cid}",
        owner=f"pi-codex-chiap02-{cid}",
        claim_revision="r1",
    )
    _age_shards(tmp_path, cid, 60)
    _evidence(tmp_path, cid, f"pi-codex-chiap08-{cid}", hours_ago=0.01)

    verdict = evaluate([_held(tmp_path)[cid]], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert verdict.reclaimable is False


def test_a_worker_for_a_DIFFERENT_card_does_not_count(tmp_path):
    """The card-id match must be about THIS card, or any busy worker on the
    fleet would keep every claim alive."""
    store = _store(tmp_path)
    cid = _card(store, "abandoned", id="aaaa1111")
    store.append_event(cid, "claim", "dead", owner="dead", claim_revision="r1")
    _age_shards(tmp_path, cid, 200)
    _evidence(tmp_path, cid, "pi-codex-chiap01-bbbb2222", hours_ago=0.01)

    verdict = evaluate([_held(tmp_path)[cid]], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert verdict.reclaimable is True


def test_a_naive_timestamp_is_resolved_as_utc(tmp_path):
    """`append_event` merges its payload over the envelope, so a caller can
    land a naive `ts`. Resolving it host-local shifts apparent idleness by
    the host's UTC offset: 9 hours on an Asian host, 14 at UTC+14."""
    from datetime import datetime, timedelta, timezone

    from skcapstone.fleet.claim_expiry import _parse_ts

    naive = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None)
    aware = naive.replace(tzinfo=timezone.utc)
    assert _parse_ts(naive.isoformat()) == pytest.approx(aware.timestamp(), abs=1)
    assert _parse_ts("not a timestamp") == 0.0
    assert _parse_ts(None) == 0.0


def test_a_dispatcher_worker_liveness_link_counts_as_the_OWNER_being_alive(tmp_path):
    """The strongest liveness signal in the estate, and it is not written by
    the worker.

    `skfleet-rotate` emits a `worker_liveness` link every 2 to 3 minutes for
    each worker it observes alive. Its `writer` is the dispatcher, and the
    OWNER's name is inside `link_value` as "<owner>|<claim_revision>".

    Measured on chi 2026-09-18: 133 of 133 evidence events across a sample of
    live cards were exactly this, with the workers themselves writing nothing.
    Matching only on `writer` therefore ignored it, and a worker running past
    the TTL would have had its card reclaimed while the dispatcher was
    actively recording it alive.
    """
    store = _store(tmp_path)
    cid = _card(store, "long-running worker")
    owner = "pi-codex-chiap01-deadbeef"
    store.append_event(cid, "claim", owner, owner=owner, claim_revision="rev-1")
    _age_shards(tmp_path, cid, 200)

    d = tmp_path / "coordination" / "card_events"
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    (d / "chiap01.jsonl").write_text(
        json.dumps(
            {
                "card_id": cid,
                "action": "link",
                "writer": "skfleet-rotate",
                "link_key": "worker_liveness",
                "link_value": f"{owner}|rev-1",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    verdict = evaluate([_held(tmp_path)[cid]], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert (
        verdict.reclaimable is False
    ), "the dispatcher just saw this worker alive; the card must not expire"
    assert verdict.idle_seconds < HOUR


def test_a_liveness_link_naming_a_DIFFERENT_owner_does_not_count(tmp_path):
    """The attribution must be to the owner the link names, or any liveness
    ping anywhere would keep every card alive."""
    store = _store(tmp_path)
    cid = _card(store, "abandoned")
    store.append_event(cid, "claim", "real-owner", owner="real-owner", claim_revision="r1")
    _age_shards(tmp_path, cid, 200)

    d = tmp_path / "coordination" / "card_events"
    d.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone

    (d / "chiap01.jsonl").write_text(
        json.dumps(
            {
                "card_id": cid,
                "action": "link",
                "writer": "skfleet-rotate",
                "link_key": "worker_liveness",
                "link_value": "someone-else|r9",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    verdict = evaluate([_held(tmp_path)[cid]], now=time.time(), ttl_seconds=48 * HOUR)[0]
    assert verdict.reclaimable is True
