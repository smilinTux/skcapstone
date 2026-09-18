"""The dispatcher returns a card whose blockers have all already finished.

``_blocker_change_epoch`` funds a retry when the blocker CHANGES after the
BLOCKED verdict. A blocker that was already terminal when the verdict landed
can never change again, so that card is parked forever. The sweep tested here
is the repair: it names those cards, and only those, and the caller turns each
one into a durable attributed ``reopen``.

Loaded by AST extraction, the same way the other dispatcher-function tests in
this suite work, so the real source is under test without running a rotation.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from skcapstone.blocker_referent import settled_blocker_reopen

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"

DONE = {"state": "complete", "human_gated": False, "outcome_blocked": False, "completed_at": 10.0}


def _load(namespace: dict):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    wanted = {"settled_blocker_reopens"}
    body = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in body} == wanted
    namespace.setdefault("os", os)
    namespace.setdefault("re", re)
    namespace.setdefault("settled_blocker_reopen", settled_blocker_reopen)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["settled_blocker_reopens"]


def _sweep(
    outcomes,
    reasons,
    facts,
    *,
    states=None,
    events=None,
    limit=None,
    cards_on_disk=None,
):
    """Run the real sweep against an in-memory board."""
    states = states or {}
    on_disk = set(cards_on_disk if cards_on_disk is not None else outcomes)
    namespace = {
        "CARDS": "/cards",
        "_load_outcomes": lambda: outcomes,
        "_latest_blocked_reason": lambda cid, ts, val: reasons.get(cid),
        "_settled_blocker_facts": lambda ref: facts.get(ref, {"state": "missing"}),
        "lifecycle_state": lambda cid: states.get(cid, "open"),
        "event_rows": lambda cid: (events or {}).get(cid, []),
    }
    namespace["os"] = type(
        "_os",
        (),
        {
            "path": type(
                "_p",
                (),
                {
                    "isdir": staticmethod(lambda p: Path(p).name in on_disk),
                    "join": staticmethod(os.path.join),
                },
            )
        },
    )
    return _load(namespace)(limit)


BLOCKED = ("2026-09-18T00:00:00Z", "BLOCKED. blocked_on=dependency referent=card:bbbbbbbb")


def test_a_card_whose_blocker_already_finished_is_returned():
    returned, held = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
    )
    assert [cid for cid, _ in returned] == ["aaaaaaaa"]
    assert returned[0][1]["transition_id"].startswith("blocker-settled:")
    assert held == {}


def test_a_card_that_is_not_blocked_is_never_touched():
    returned, _ = _sweep(
        {"aaaaaaaa": ("2026-09-18T00:00:00Z", "PASS")},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
    )
    assert returned == []


def test_a_verdict_that_only_mentions_blocked_in_prose_is_not_a_block():
    returned, _ = _sweep(
        {"aaaaaaaa": ("2026-09-18T00:00:00Z", "PASS; supersedes the earlier BLOCKED verdict")},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
    )
    assert returned == []


def test_a_human_hold_is_left_for_a_human():
    returned, held = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("human", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
    )
    assert returned == []
    assert held == {"category:human": 1}


def test_a_block_with_no_parsable_reason_is_left_for_an_operator():
    returned, held = _sweep({"aaaaaaaa": BLOCKED}, {}, {})
    assert returned == []
    assert held == {"category:unparsed": 1}


def test_a_done_or_voided_card_is_not_returned_to_the_pool():
    for state in ("complete", "void"):
        returned, _ = _sweep(
            {"aaaaaaaa": BLOCKED},
            {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
            {"bbbbbbbb": DONE},
            states={"aaaaaaaa": state},
        )
        assert returned == [], state


def test_a_card_with_no_directory_on_disk_is_skipped():
    returned, _ = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
        cards_on_disk=set(),
    )
    assert returned == []


def test_a_card_already_returned_for_this_generation_is_not_returned_again():
    """Idempotency. The token names the blocker generation, so a second run,
    another host, or a worker re-blocking on the same finished referent all
    hit the same event and append nothing."""
    first, _ = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
    )
    token = first[0][1]["transition_id"]
    returned, held = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
        events={"aaaaaaaa": [{"action": "reopen", "transition_id": token}]},
    )
    assert returned == []
    assert held == {"already-returned": 1}


def test_an_unrelated_reopen_does_not_suppress_the_return():
    returned, _ = _sweep(
        {"aaaaaaaa": BLOCKED},
        {"aaaaaaaa": ("dependency", ("card:bbbbbbbb",))},
        {"bbbbbbbb": DONE},
        events={"aaaaaaaa": [{"action": "reopen", "transition_id": "blocker-settled:deadbeef"}]},
    )
    assert [cid for cid, _ in returned] == ["aaaaaaaa"]


def test_the_batch_is_capped_so_a_backlog_does_not_flood_the_fleet():
    ids = ["card%04d" % n for n in range(10)]
    returned, _ = _sweep(
        {cid: BLOCKED for cid in ids},
        {cid: ("dependency", ("card:bbbbbbbb",)) for cid in ids},
        {"bbbbbbbb": DONE},
        limit=3,
    )
    assert len(returned) == 3


def test_holds_are_counted_by_reason_so_an_operator_can_see_the_shape():
    returned, held = _sweep(
        {"aaaaaaaa": BLOCKED, "cccccccc": BLOCKED, "dddddddd": BLOCKED},
        {
            "aaaaaaaa": ("human", ("card:bbbbbbbb",)),
            "cccccccc": ("capability", ("ac:3",)),
            "dddddddd": ("dependency", ("card:eeeeeeee",)),
        },
        {"bbbbbbbb": DONE},
    )
    assert returned == []
    assert held == {"category:human": 1, "category:capability": 1, "referent-missing": 1}
