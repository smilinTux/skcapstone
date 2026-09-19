"""A claim that opened and closed with nothing written is not an attempt.

MEASURED ON THE LIVE CHI CLUSTER, 2026-09-18/19. 107 cards were frozen by the
claim ceiling and a full triage found that essentially none of them had failed:
claims and releases matched almost exactly, with zero completions.

The clearest case, card 0aec5a64. Its own seat wrote 122 claims and 120
release_claims into
``cards/0aec5a64/events/pi-glm-chiap01-0aec5a64@chiap01.jsonl``:

    07:30:23 claim          owner=pi-glm-chiap01-0aec5a64 revision=f1a09f62...
    07:30:41 release_claim  released_owner=pi-glm-chiap01-0aec5a64
    07:35:24 claim
    07:35:47 release_claim
    07:40:24 claim
    07:40:48 release_claim                      ... 120 times

Median hold 21.4s (p25 19.2, p75 23.6). Median gap to the next claim 276.8s,
which is the five-minute dispatch timer. Nothing whatever was written on the
card between any claim and its release. The seat picked the card up, died in
twenty seconds, released its own claim, and was handed the same card again on
the next tick until the ceiling froze it at five.

WHY THIS IS ONE BUG AND NOT FOUR. The triage separated seat self-reclaim,
worker-death churn, liveness-reaper churn and generic claim/release churn.
They differ only in WHO wrote the release. In every one of them the card was
held briefly and nothing was written under the hold, so the ceiling, which is
monotonic and never self-clears, converted a fleet-side defect into a
permanent freeze of the card. The predicate below is therefore keyed on the
hold, not on the releaser.

THE FENCE THAT MUST NOT MOVE. 06a95c23 is the one genuine runaway: 402 claims
against 8 releases. 394 of its claims were superseded by another claim without
ever closing, so they have no window, and an open claim proves nothing about
whether work happened under it. Those 394 stay charged and the card stays
locked. ``test_runaway_card_hits_the_ceiling`` and
``test_the_402_claim_runaway_stays_locked_without_an_amnesty`` are that fence;
they still pass untouched.
"""

from __future__ import annotations

import ast
import bisect
import collections
import datetime
import json
import os
import re
from pathlib import Path

from skcapstone.fleet import churn_breaker as cb

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_claim_ceiling_hit",
    "_claim_amnesty_epoch",
    "_countable_claims",
    "_work_epochs",
    "_work_between",
    "_fold_key",
    "_ts_epoch",
}
CONSTANTS = {
    "_MAX_CLAIMS",
    "_AMNESTY_VALUE_RE",
    "_CLAIM_BOOKKEEPING",
    "_CLAIM_CLOSING",
    "_REAP_WRITER",
}

CID = "0aec5a64"
SEAT = "pi-glm-chiap01-0aec5a64"
REAPER = "fleet-liveness-reaper"


class _AnyCard(dict):
    """The evidence overlay, answering for whichever card id a test names."""

    def __init__(self, rows: list[dict]) -> None:
        super().__init__()
        self._rows = rows

    def get(self, key, default=None):  # noqa: D102 - dict protocol
        return list(self._rows)


def _ns(card_events: list[dict], evidence: list[dict] | None = None) -> dict:
    """Extract the pure ceiling seam from the script without running it."""
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
        "bisect": bisect,
        "datetime": datetime,
        "collections": collections,
        "acts": lambda cid: collections.Counter(e.get("action") for e in card_events),
        "event_rows": lambda cid: list(card_events),
        "_load_evidence_events": lambda: _AnyCard(list(evidence or [])),
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    missing = FUNCTIONS - namespace.keys()
    assert not missing, f"unworked-claim seam missing from script: {sorted(missing)}"
    return namespace


BASE = datetime.datetime(2026, 9, 9, 7, 30, tzinfo=datetime.timezone.utc)


def _at(minutes: float) -> str:
    return (BASE + datetime.timedelta(minutes=minutes)).isoformat()


def _cycles(n: int, hold_s: float = 21.4, period_s: float = 298.0, writer: str = SEAT):
    """The measured 0aec5a64 shape: claim, then the seat's own release."""
    rows: list[dict] = []
    for i in range(n):
        opened = BASE + datetime.timedelta(seconds=period_s * i)
        rows.append(
            {
                "action": "claim",
                "writer": writer,
                "owner": SEAT,
                "claim_revision": "rev%03d" % i,
                "ts": opened.isoformat(),
            }
        )
        rows.append(
            {
                "action": "release_claim",
                "writer": writer,
                "released_owner": SEAT,
                "expected_claim_revision": "rev%03d" % i,
                "ts": (opened + datetime.timedelta(seconds=hold_s)).isoformat(),
            }
        )
    return rows


# ----------------------------------------------------------------------
# The defect.
# ----------------------------------------------------------------------


def test_the_measured_0aec5a64_loop_is_not_charged():
    """120 twenty-second holds with nothing written under any of them."""
    ns = _ns(_cycles(120))
    assert ns["_countable_claims"](CID, 120) == 0
    assert ns["_claim_ceiling_hit"](CID) is False


def test_only_the_worked_claims_are_charged():
    rows = _cycles(9)
    # One of the nine holds actually produced something on the card.
    rows.insert(
        7,
        {
            "action": "link",
            "writer": SEAT,
            "link_key": "pr",
            "link_value": "https://github.com/smilinTux/skcapstone/pull/1",
            "ts": (BASE + datetime.timedelta(seconds=298 * 3 + 10)).isoformat(),
        },
    )
    ns = _ns(rows)
    assert ns["_countable_claims"](CID, 9) == 1


def test_a_release_by_the_reaper_is_the_same_bug_not_a_different_one():
    """Worker-death churn differs only in who wrote the release."""
    rows = _cycles(12, hold_s=588.0, period_s=900.0, writer=REAPER)
    evidence = [
        {
            "card_id": CID,
            "action": "verdict",
            "verdict": "WORKER_DIED",
            "writer": REAPER,
            "ts": rows[2 * i]["ts"],
        }
        for i in range(12)
    ]
    ns = _ns(rows, evidence)
    assert ns["_countable_claims"](CID, 12) == 0


# ----------------------------------------------------------------------
# The fences. Each is how a runaway would launder itself through this.
# ----------------------------------------------------------------------


def test_the_402_claim_runaway_stays_locked():
    """06a95c23: 402 claims, 8 releases. 394 holds never closed."""
    rows: list[dict] = []
    for i in range(402):
        rows.append({"action": "claim", "writer": SEAT, "ts": _at(i), "claim_revision": "r%d" % i})
        if i < 8:
            rows.append({"action": "release_claim", "writer": SEAT, "ts": _at(i + 0.1)})
    ns = _ns(rows)
    assert ns["_countable_claims"]("06a95c23", 402) == 394
    assert ns["_claim_ceiling_hit"]("06a95c23") is True


def test_an_open_claim_is_charged():
    """No release means no closed window, and an open hold proves nothing."""
    ns = _ns([{"action": "claim", "writer": SEAT, "ts": _at(i)} for i in range(6)])
    assert ns["_countable_claims"](CID, 6) == 6
    assert ns["_claim_ceiling_hit"](CID) is True


def test_a_claim_with_an_unparseable_timestamp_is_charged():
    rows = [
        {"action": "claim", "writer": SEAT, "ts": "not-a-time"},
        {"action": "release_claim", "writer": SEAT, "ts": _at(1)},
    ] * 6
    ns = _ns(rows)
    assert ns["_countable_claims"](CID, 6) == 6


def test_work_in_the_overlay_store_alone_charges_the_claim():
    """A worker's PASS lands only in coordination/card_events. Read both."""
    rows = _cycles(6)
    evidence = [
        {
            "card_id": CID,
            "action": "link",
            "link_key": "verdict",
            "link_value": "PASS_FOR_REVIEW",
            "writer": SEAT,
            "ts": (BASE + datetime.timedelta(seconds=10)).isoformat(),
        }
    ]
    ns = _ns(rows, evidence)
    assert ns["_countable_claims"](CID, 6) == 1


def test_the_reapers_own_rows_are_bookkeeping_not_work():
    """A WORKER_DIED verdict describes the WORKER. It is not work on the card."""
    rows = _cycles(6, hold_s=588.0, period_s=900.0)
    evidence = [
        {
            "card_id": CID,
            "action": "link",
            "link_key": "worker_died",
            "link_value": "owner=%s claim_revision=rev00%d" % (SEAT, i),
            "writer": REAPER,
            "ts": (BASE + datetime.timedelta(seconds=900 * i + 100)).isoformat(),
        }
        for i in range(6)
    ]
    ns = _ns(rows, evidence)
    assert ns["_countable_claims"](CID, 6) == 0


def test_a_mero_observation_under_the_hold_is_bookkeeping():
    rows = _cycles(6, hold_s=588.0, period_s=900.0)
    rows.append(
        {
            "action": "mero_observation",
            "writer": "mero",
            "state": "worker_absent_after_quorum",
            "ts": (BASE + datetime.timedelta(seconds=100)).isoformat(),
        }
    )
    rows.sort(key=lambda e: e["ts"])
    ns = _ns(rows)
    assert ns["_countable_claims"](CID, 6) == 0


def test_a_completed_card_is_still_never_ceilinged():
    rows = _cycles(99) + [{"action": "complete", "writer": SEAT, "ts": _at(9999)}]
    ns = _ns(rows)
    assert ns["_claim_ceiling_hit"](CID) is False


def test_amnesty_and_an_unworked_hold_never_forgive_one_claim_twice():
    rows = _cycles(6)
    evidence = [
        {
            "card_id": CID,
            "action": "link",
            "link_key": "claim_amnesty",
            "link_value": "PR-778|seat-self-reclaim-loop",
            "writer": "chef",
            "ts": _at(9999),
        }
    ]
    ns = _ns(rows, evidence)
    assert ns["_countable_claims"](CID, 6) == 0


# ----------------------------------------------------------------------
# The churn breaker is the second gate on the same claim, and it does NOT
# share this rule. It still charges a bare claim/release cycle, because an
# unworked hold is not a degenerate stand-in for the breaker's signal, it IS
# the signal: cards reach the breaker precisely because no agent CAN work
# them, so nothing is ever written under the hold. Card 83e498b6, the
# breaker's own motivating card, counts 57 attempts under this rule and 11
# under the ceiling's; a pure claim/release loop counts 0 and could never
# trip the breaker at all.
#
# The two gates are allowed to disagree because their refusals are not the
# same act. The ceiling is monotonic and clears only by an operator-written
# claim_amnesty link. The breaker is default-off, and a refusal clears the
# moment anyone records one blocker sentence -- which is the entire point of
# the gate. See tests/test_churn_breaker.py for the breaker's own contract.
# What is kept here is the fence they DO share: an open claim is charged by
# both.
# ----------------------------------------------------------------------


def _write(home: Path, rows: list[dict], evidence: list[dict] | None = None) -> None:
    events = home / "cards" / CID / "events"
    events.mkdir(parents=True, exist_ok=True)
    (events / "shard.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    overlay = home / "coordination" / "card_events"
    overlay.mkdir(parents=True, exist_ok=True)
    (overlay / "overlay.jsonl").write_text(
        "".join(json.dumps(dict(row, card_id=CID)) + "\n" for row in (evidence or [])),
        encoding="utf-8",
    )


def test_churn_breaker_still_charges_an_open_claim(tmp_path):
    rows = [
        {"action": "claim", "writer": SEAT, "owner": "seat-%d" % i, "ts": _at(i)} for i in range(6)
    ]
    _write(tmp_path, rows)
    assert cb.read_signature(tmp_path, CID).claim_attempts == 6
