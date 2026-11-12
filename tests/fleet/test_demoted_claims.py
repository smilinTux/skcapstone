"""Demoted claims are released only when idle, ready and not the owner's current task."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from skcapstone.fleet import demoted_claims as dc

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
OLD = (NOW - timedelta(days=2)).isoformat()
FRESH = (NOW - timedelta(minutes=30)).isoformat()


def _card(card_id, owner="jarvis", status="ready", revision="r1", archived=False):
    return SimpleNamespace(
        id=card_id,
        owner=owner,
        status=SimpleNamespace(value=status),
        meta={"_claim_revision": revision} if revision else {},
        archived=archived,
    )


class _Store:
    def __init__(self, cards, events):
        self.cards, self.events = cards, events

    def list_cards(self):
        return self.cards

    def _read_events(self, card_id):
        return self.events.get(card_id, [])


def _claim(owner="jarvis", revision="r1", ts=OLD):
    return {"action": "claim", "claim_revision": revision, "owner": owner, "ts": ts}


def test_only_idle_demoted_non_worker_claims_are_selected():
    cards = [
        _card("aa000001"),
        _card("aa000002", status="doing"),
        _card("aa000003", owner="pi-glm-builder-node-chiap01-aa000003"),
        _card("aa000004", owner="niobe-requal-aa000004"),
        _card("aa000005"),
        _card("aa000006"),
        _card("aa000007"),
        _card("aa000008", revision=None),
        _card("aa000009", archived=True),
        _card("aa00000a", owner="niobe"),
    ]
    events = {
        "aa000001": [_claim(), {"action": "move", "writer": "jarvis", "ts": OLD}],
        "aa000005": [_claim(ts=FRESH)],
        "aa000006": [_claim(), {"action": "link", "writer": "jarvis", "ts": FRESH}],
        "aa000007": [_claim(revision="older")],
        "aa00000a": [_claim(owner="niobe")],
    }
    current = {"jarvis": "ff000001", "niobe": "aa00000a"}
    selected = list(dc.demoted_claims(_Store(cards, events), current, now=NOW))
    # aa00000a is niobe's current task; everything else is active, fresh,
    # a worker or requal claim, unbound, archived or a different generation.
    assert selected == [("aa000001", "jarvis", "r1")]


def test_release_uses_exact_generation_restores_ready_and_respects_limit(monkeypatch):
    cards = [_card("bb%06d" % n) for n in range(6)]
    events = {card.id: [_claim()] for card in cards}
    import skcoord.card_store as card_store
    import skcoord.coordination as coordination

    monkeypatch.setattr(card_store, "CardStore", lambda home: _Store(cards, events))
    monkeypatch.setattr(
        coordination,
        "Board",
        lambda home: SimpleNamespace(load_agents=lambda: []),
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[2:])
        refused = argv[2] == "release-claim" and argv[3] == "bb000001"
        return SimpleNamespace(returncode=1 if refused else 0)

    results = dc.release_demoted_claims("/home", limit=3, now=NOW, run=run)
    assert [row["state"] for row in results] == ["released", "refused", "released"]
    assert calls[0] == [
        "release-claim",
        "bb000000",
        "--owner",
        "jarvis",
        "--expected-claim-revision",
        "r1",
        "--agent",
        "niobe",
    ]
    assert calls[1] == ["move", "bb000000", "ready", "--agent", "niobe"]
    # A refused release is never followed by a move.
    assert ["move", "bb000001", "ready", "--agent", "niobe"] not in calls
