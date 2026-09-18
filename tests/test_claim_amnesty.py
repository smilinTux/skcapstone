"""Bounded claim-ceiling amnesty: claims a FIXED defect burned stop counting.

The ceiling's counter is monotonic over an append-only ledger, so a card that
churned because of a fleet defect (a worker-death storm, ownership
repartition churn) stays locked out forever after the defect is fixed, and
the only global escape (raising SKFLEET_MAX_CLAIMS) would also free the one
genuine runaway the ceiling exists for. The amnesty is one operator-granted
link event NAMING the fixed defect; only claims NEWER than the latest
well-formed amnesty are charged against _MAX_CLAIMS. Every branch fails
closed: no amnesty, a causeless amnesty, or a timestampless amnesty leaves
the card exactly as locked as today, and a wrongly amnestied card burns at
most _MAX_CLAIMS more claims and re-locks.
"""

from __future__ import annotations

import ast
import collections
import datetime
import json
import os
import re
import time
from pathlib import Path

from skcoord.card_store import CardCore, CardStore

from skcapstone.fleet import churn_breaker as cb

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_claim_ceiling_hit",
    "_claim_amnesty_epoch",
    "_countable_claims",
    "_fold_key",
    "_ts_epoch",
}
CONSTANTS = {"_MAX_CLAIMS", "_AMNESTY_VALUE_RE"}

CID = "aabbccdd"


def _ns(card_events: list[dict], evidence: list[dict]) -> dict:
    """Extract the ceiling + amnesty seam from the script without running it."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {
        "os": os,
        "re": re,
        "datetime": datetime,
        "collections": collections,
        "acts": lambda cid: collections.Counter(e.get("action") for e in card_events),
        "event_rows": lambda cid: list(card_events),
        "_load_evidence_events": lambda: {CID: list(evidence)},
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    missing = FUNCTIONS - namespace.keys()
    assert not missing, f"amnesty seam missing from script: {sorted(missing)}"
    return namespace


def _claims(n: int, start: str = "2026-09-01T00:00:00+00:00") -> list[dict]:
    base = datetime.datetime.fromisoformat(start)
    return [
        {"action": "claim", "ts": (base + datetime.timedelta(minutes=i)).isoformat()}
        for i in range(n)
    ]


def _amnesty(
    ts: str | None,
    value: object = "PR-778|ownership-repartition-churn",
    key: str = "claim_amnesty",
) -> dict:
    event = {"action": "link", "link_key": key, "link_value": value, "writer": "chef"}
    if ts is not None:
        event["ts"] = ts
    return event


AMNESTY_TS = "2026-09-05T00:00:00+00:00"
POST = "2026-09-10T00:00:00+00:00"


# ----------------------------------------------------------------------
# Requirement 1: fail closed when absent or broken.
# ----------------------------------------------------------------------


def test_no_amnesty_is_todays_behavior_six_claims_block():
    ns = _ns(_claims(6), [])
    assert ns["_claim_ceiling_hit"](CID) is True
    ns = _ns(_claims(5), [])
    assert ns["_claim_ceiling_hit"](CID) is False


def test_timestampless_amnesty_fails_closed():
    ns = _ns(_claims(6), [_amnesty(None)])
    assert ns["_claim_ceiling_hit"](CID) is True


def test_malformed_amnesty_events_fail_closed():
    malformed = [
        _amnesty("not-a-timestamp"),
        _amnesty(AMNESTY_TS, value=None),
        _amnesty(AMNESTY_TS, value=778),
        _amnesty(AMNESTY_TS, key="claim_amnestyy"),
        {"action": "link"},
    ]
    ns = _ns(_claims(6), malformed)
    assert ns["_claim_ceiling_hit"](CID) is True


def test_claim_with_unparseable_timestamp_is_charged_never_forgiven():
    claims = [{"action": "claim"} for _ in range(6)]
    ns = _ns(claims, [_amnesty(AMNESTY_TS)])
    assert ns["_claim_ceiling_hit"](CID) is True


# ----------------------------------------------------------------------
# Requirement 2: the amnesty must name the defect it forgives.
# ----------------------------------------------------------------------


def test_amnesty_with_no_named_defect_is_refused():
    for value in ("", "   ", "amnesty", "reset please", "PR-778|", "|orphan-cause", "|"):
        ns = _ns(_claims(6), [_amnesty(AMNESTY_TS, value=value)])
        assert ns["_claim_ceiling_hit"](CID) is True, f"causeless value admitted: {value!r}"


# ----------------------------------------------------------------------
# Requirement 3: only claims newer than the LATEST amnesty count.
# ----------------------------------------------------------------------


def test_valid_amnesty_counts_only_post_amnesty_claims():
    """The Sep-9 SKLEGAL shape: 40 defect-era claims forgiven, 2 real ones counted."""
    events = _claims(40) + [{"action": "claim", "ts": POST}, {"action": "claim", "ts": POST}]
    ns = _ns(events, [_amnesty(AMNESTY_TS)])
    assert ns["_countable_claims"](CID, 42) == 2
    assert ns["_claim_ceiling_hit"](CID) is False


def test_only_the_latest_amnesty_counts():
    first = "2026-09-02T00:00:00+00:00"
    events = _claims(6) + _claims(6, start="2026-09-03T00:00:00+00:00")
    ns = _ns(events, [_amnesty(first), _amnesty(AMNESTY_TS, value="PR-790|death-storm")])
    assert ns["_countable_claims"](CID, 12) == 0
    assert ns["_claim_ceiling_hit"](CID) is False
    # The older amnesty alone forgives only the first six.
    ns = _ns(events, [_amnesty(first)])
    assert ns["_countable_claims"](CID, 12) == 6
    assert ns["_claim_ceiling_hit"](CID) is True


# ----------------------------------------------------------------------
# Requirement 4: bounded by construction, a wrong amnesty re-locks.
# ----------------------------------------------------------------------


def test_card_that_burns_five_more_claims_after_amnesty_relocks():
    forgiven = _claims(30)
    ns = _ns(forgiven + _claims(5, start=POST), [_amnesty(AMNESTY_TS)])
    assert ns["_claim_ceiling_hit"](CID) is False
    ns = _ns(forgiven + _claims(6, start=POST), [_amnesty(AMNESTY_TS)])
    assert ns["_claim_ceiling_hit"](CID) is True


def test_the_402_claim_runaway_stays_locked_without_an_amnesty():
    """06a95c23 is the case the ceiling exists for. No amnesty, no exit."""
    ns = _ns(_claims(402), [])
    assert ns["_claim_ceiling_hit"](CID) is True


# ----------------------------------------------------------------------
# The sibling gate: the claim-time churn breaker honours the same fence,
# or an amnestied card would clear the ceiling and be refused at the claim.
# ----------------------------------------------------------------------


def _ready_card(home: Path, card_id: str) -> CardStore:
    store = CardStore(home)
    store.create(CardCore(id=card_id, kind="task", title="amnesty fixture", created_by="test"))
    store.append_event(card_id, "move", "test-seat", column="ready")
    return store


def _cycle(store: CardStore, card_id: str, owner: str) -> None:
    revision = store.append_event(card_id, "claim", owner, owner=owner)["event_id"]
    store.append_event(
        card_id,
        "release_claim",
        owner,
        released_owner=owner,
        expected_claim_revision=revision,
        abandon_reason="dependency-unsatisfied",
    )


def _grant_amnesty(home: Path, card_id: str, value: str) -> None:
    events = home / "coordination" / "card_events"
    events.mkdir(parents=True, exist_ok=True)
    time.sleep(0.002)
    row = {
        "card_id": card_id,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "action": "link",
        "link_key": "claim_amnesty",
        "link_value": value,
        "writer": "chef",
    }
    with (events / "operator.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    time.sleep(0.002)


def test_churn_breaker_counts_only_post_amnesty_attempts(tmp_path) -> None:
    card_id = f"{201:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c", "seat-d", "seat-e", "seat-f"):
        _cycle(store, card_id, owner)
    _grant_amnesty(tmp_path, card_id, "PR-778|ownership-repartition-churn")
    _cycle(store, card_id, "seat-g")

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.claim_attempts == 1
    assert signature.distinct_owners == 1
    verdict = cb.evaluate(signature, max_claim_attempts=5, max_owners=3)
    assert verdict.refuse is False
    assert verdict.reason == "under-threshold"


def test_churn_breaker_refuses_a_causeless_amnesty(tmp_path) -> None:
    card_id = f"{202:08x}"
    store = _ready_card(tmp_path, card_id)
    for owner in ("seat-a", "seat-b", "seat-c", "seat-d", "seat-e", "seat-f"):
        _cycle(store, card_id, owner)
    _grant_amnesty(tmp_path, card_id, "reset please")
    _cycle(store, card_id, "seat-g")

    signature = cb.read_signature(tmp_path, card_id)

    assert signature.claim_attempts == 7
    assert signature.distinct_owners == 7
    assert cb.claim_amnesty_epoch(tmp_path, card_id) == 0.0


def test_churn_breaker_amnesty_epoch_is_zero_without_a_timestamp(tmp_path) -> None:
    card_id = f"{203:08x}"
    events = tmp_path / "coordination" / "card_events"
    events.mkdir(parents=True)
    row = {
        "card_id": card_id,
        "action": "link",
        "link_key": "claim_amnesty",
        "link_value": "PR-778|ownership-repartition-churn",
    }
    (events / "operator.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert cb.claim_amnesty_epoch(tmp_path, card_id) == 0.0
