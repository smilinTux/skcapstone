"""A builder candidate is withheld from the local lanes only while HELD.

PR #635 (2026-09-11) removed every `eligible()` card from every regular host
unconditionally, so a channel built to feed one idle box its first four cards
became the exclusive owner of an entire card class. Measured on the chi estate
2026-09-19: 14-16 of a 19-20 card pool parked on `node-ziowk01`'s four slots
(13 `builders-at-capacity` refusals in one tick) while `owner_free` advertised
54 free local seats, and two of the parked cards could never be dispatched at
all (`026a08d9`, terminal failed at the attempt ceiling; `23554ec7`, a binding
`offer()` raises on before it writes anything).

The contract these tests pin: a card is withheld ONLY while the builder path
actively holds it, meaning a live non-terminal dispatch request or an offer
still inside its lease. Terminal, unplaceable, or at-capacity means the card
goes to the local lanes.
"""

from __future__ import annotations

import ast
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch, sknoded, store

ROTATE = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"
NOW = datetime(2026, 9, 19, 6, 55, tzinfo=timezone.utc)

# Real card ids from the chi board, 2026-09-19. 026a08d9 was terminal-failed on
# node-ziowk01; 23554ec7 has a binding no request can be written for.
TERMINAL_CARD = "026a08d9"
UNOFFERABLE_CARD = "23554ec7"
RUNNING_CARD = "9968f114"


@pytest.fixture(autouse=True)
def _clear_process_registry(monkeypatch):
    builder_dispatch._PROCESSES.clear()
    monkeypatch.setenv("SKFLEET_PI", "/test/bin/pi")
    monkeypatch.setattr(builder_dispatch.CardStore, "fold", lambda *_args: _folded())
    yield
    builder_dispatch._PROCESSES.clear()


def _card(card_id: str = "24b00003") -> dict:
    return {
        "id": card_id,
        "meta": {
            "repository": "https://github.com/smilinTux/skcapstone.git",
            "base_ref": "main",
            "base_revision": "9cc415465d6bacc22b51b09a3c861c61f0823d45",
        },
    }


def _folded(**values) -> SimpleNamespace:
    defaults = {
        "id": "24b00003",
        "owner": None,
        "meta": dict(_card()["meta"]),
        "labels": ["sk-m", "source-only"],
        "status": SimpleNamespace(value="doing"),
        "links": {},
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _node(paths, operator, noded41) -> None:
    store.write_spec(
        paths,
        "node",
        "node-ziowk01",
        {"role": "builder-standby", "actuate": True, "cordoned": False},
        writer=operator,
        labels={"host": "ziowk01"},
    )
    sknoded.run_once(paths, "node-ziowk01")


def _offer(paths, card_id: str, *, now: datetime = NOW) -> dict:
    """Place one real request through offer(), never a hand-written file."""
    request = builder_dispatch.offer(
        paths,
        _card(card_id),
        ["sk-m", "source-only"],
        writer=store.Writer(role="scheduler", node="niobe", identity="capauth:niobe"),
        now=now,
    )
    assert request is not None
    return request


def _rotate_partition():
    """Lift `_builder_partition` out of the shipped rotation script.

    The withhold decision that actually runs on chiap01-08 lives in a script,
    not an importable module, so this tests the source that runs rather than a
    paraphrase of it (the pattern `tests/test_seat_placement.py` established).
    """
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_builder_partition"
    ]
    assert functions, "the rotation script must still define _builder_partition"
    namespace: dict = {}
    exec(  # noqa: S102 - lifting the shipped source is the point of this test
        compile(ast.Module(body=functions, type_ignores=[]), str(ROTATE), "exec"),
        namespace,
    )
    return namespace["_builder_partition"]


def _row(card_id: str) -> tuple:
    """Return a pool row shaped like the rotation's, card id at index 2."""
    return (0, "doing", card_id, _card(card_id)["meta"], ["sk-m", "source-only"])


# --------------------------------------------------------------------------
# held_card_ids(): what the builder path actually holds
# --------------------------------------------------------------------------


def test_a_fresh_offer_is_held(paths, operator, noded41) -> None:
    """The offer-to-remote-claim window is the one window withholding exists for."""
    _node(paths, operator, noded41)
    _offer(paths, "24b00003")
    assert builder_dispatch.held_card_ids(paths, now=NOW) == {"24b00003"}


def test_a_running_dispatch_is_held(paths, operator, noded41) -> None:
    """A worker mid-flight keeps its card away from every local lane."""
    _node(paths, operator, noded41)
    request = _offer(paths, RUNNING_CARD)
    builder_dispatch._write_status(paths, "node-ziowk01", request, "running")
    later = NOW + timedelta(seconds=builder_dispatch.LEASE_SECONDS * 10)
    assert builder_dispatch.held_card_ids(paths, now=later) == {RUNNING_CARD}


def test_a_terminal_dispatch_is_not_held(paths, operator, noded41) -> None:
    """026a08d9: failed at the attempt ceiling, and withheld from 54 free seats."""
    _node(paths, operator, noded41)
    request = _offer(paths, TERMINAL_CARD)
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "failed",
        attempt=builder_dispatch.MAX_ATTEMPTS,
        retryable=False,
        error="existing workspace does not match exact source binding",
    )
    assert builder_dispatch.held_card_ids(paths, now=NOW) == set()
    # And the builder path agrees it will never take it as things stand.
    assert str(
        builder_dispatch.decline_reason(paths, _card(TERMINAL_CARD), ["sk-m", "source-only"])
    ).startswith("terminal:")


@pytest.mark.parametrize("state", sorted(builder_dispatch.TERMINAL_STATES))
def test_no_terminal_state_holds_a_card(paths, operator, noded41, state) -> None:
    """completed, blocked, failed and stale all release the card."""
    _node(paths, operator, noded41)
    request = _offer(paths, "24b00003")
    builder_dispatch._write_status(paths, "node-ziowk01", request, state)
    assert builder_dispatch.held_card_ids(paths, now=NOW) == set()


def test_an_offer_that_expired_unclaimed_is_not_held(paths, operator, noded41) -> None:
    """16 of ziowk01's 41 lifetime dispatches died exactly this way."""
    _node(paths, operator, noded41)
    _offer(paths, "24b00003")
    inside = NOW + timedelta(seconds=builder_dispatch.LEASE_SECONDS - 1)
    outside = NOW + timedelta(seconds=builder_dispatch.LEASE_SECONDS + 1)
    assert builder_dispatch.held_card_ids(paths, now=inside) == {"24b00003"}
    assert builder_dispatch.held_card_ids(paths, now=outside) == set()


def test_a_card_at_capacity_is_not_held(paths, operator, noded41) -> None:
    """The 13-hit case: a full builder must not park the rest of the pool.

    Four cards fill node-ziowk01 and are held. The fifth is refused with
    `builders-at-capacity`, no request is ever written for it, and it is
    therefore free for a local lane -- which is the whole measured defect:
    `builders-at-capacity: node-ziowk01=4/4` logged 13 times in one tick while
    `owner_free` advertised 54 free local seats.
    """
    _node(paths, operator, noded41)
    filled = [f"24b0000{number}" for number in range(1, 5)]
    for card_id in filled:
        _offer(paths, card_id)
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    overflow = builder_dispatch.offer(
        paths, _card("24b00009"), ["sk-m", "source-only"], writer=writer, now=NOW
    )
    assert overflow is None
    assert str(
        builder_dispatch.decline_reason(paths, _card("24b00009"), ["sk-m", "source-only"])
    ).startswith("builders-at-capacity")
    held = builder_dispatch.held_card_ids(paths, now=NOW)
    assert held == set(filled)
    assert "24b00009" not in held


def test_a_card_no_request_was_ever_written_for_is_not_held(paths, operator, noded41) -> None:
    """23554ec7: offer() raises on the binding, so no request can exist."""
    _node(paths, operator, noded41)
    core = {"id": UNOFFERABLE_CARD, "meta": {"repository": "git@github.com:x/y.git"}}
    writer = store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")
    with pytest.raises(builder_dispatch.BuilderDispatchError):
        builder_dispatch.offer(paths, core, ["sk-m", "source-only"], writer=writer, now=NOW)
    assert builder_dispatch.held_card_ids(paths, now=NOW) == set()


def test_an_empty_dispatch_tree_holds_nothing(paths) -> None:
    """No builder has ever been offered anything: nothing may be withheld."""
    assert builder_dispatch.held_card_ids(paths, now=NOW) == set()


# --------------------------------------------------------------------------
# No double-claim window: every release is provably a release of NOTHING
# --------------------------------------------------------------------------


def test_a_held_card_is_never_released_while_a_node_could_still_run_it(
    paths, operator, noded41
) -> None:
    """The failure mode this change must not have.

    Exhaustive over the request/status pairs the protocol can produce: for
    every pair where the node's own consumer (`_consume_available`) would
    still pick the request up, held_card_ids() must report the card as held.
    A release in any of those states is what would put a local lane and a
    remote worker on one card.
    """
    _node(paths, operator, noded41)
    request = _offer(paths, "24b00003")
    consumable = [
        ({}, NOW),  # offered, node has not answered yet, lease live
        ({"request_id": request["request_id"], "state": "accepted"}, NOW),
        ({"request_id": request["request_id"], "state": "running"}, NOW),
        ({"request_id": request["request_id"], "state": "frozen"}, NOW),
        # A status from a SUPERSEDED generation leaves the current offer
        # outstanding, so the node may still consume it inside the lease.
        ({"request_id": "stale-generation", "state": "failed"}, NOW),
    ]
    for status, stamp in consumable:
        assert builder_dispatch.request_holds_card(request, status, stamp) is True


def test_release_only_happens_where_offer_writes_nothing(paths, operator, noded41) -> None:
    """A released card has no live request, so no builder can claim it.

    The builder path only ever claims a card it holds a live request for
    (`_consume_available` iterates requests, and claims through
    `Board.claim_task` behind `assert_claim_permitted`). This asserts the
    converse of the previous test on the same tree: every card that
    held_card_ids() releases is a card whose request is terminal, expired, or
    absent, which are exactly the three states in which the node's consumer
    skips it.
    """
    _node(paths, operator, noded41)
    live = _offer(paths, "24b00001")
    terminal = _offer(paths, "24b00002")
    builder_dispatch._write_status(paths, "node-ziowk01", terminal, "blocked", attempt=0)
    expired = _offer(paths, "24b00003")
    later = NOW + timedelta(seconds=builder_dispatch.LEASE_SECONDS + 1)

    held = builder_dispatch.held_card_ids(paths, now=later)
    assert held == set()  # the live one's lease has also run out by `later`
    assert builder_dispatch.held_card_ids(paths, now=NOW) == {"24b00001", "24b00003"}

    for request, stamp, expected in (
        (live, NOW, True),
        (expired, later, False),
        (terminal, NOW, False),
    ):
        status = (
            builder_dispatch._load(
                builder_dispatch.status_path(paths, "node-ziowk01", request["card_id"])
            )
            or {}
        )
        assert builder_dispatch.request_holds_card(request, status, stamp) is expected


def test_an_unreadable_record_is_held_not_released(paths, operator, noded41) -> None:
    """Fail closed per card: the release is the only direction with a race."""
    _node(paths, operator, noded41)
    _offer(paths, "24b00003")
    builder_dispatch.request_path(paths, "node-ziowk01", "24b00003").write_text(
        "{ not json", encoding="utf-8"
    )
    assert builder_dispatch.held_card_ids(paths, now=NOW) == {"24b00003"}


def test_an_unreadable_tree_raises_so_the_caller_can_withhold_everything(
    paths, operator, noded41, monkeypatch
) -> None:
    """A scan that cannot answer must not answer "the builder holds nothing"."""
    _node(paths, operator, noded41)
    _offer(paths, "24b00003")

    def _deny(self):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "iterdir", _deny)
    with pytest.raises(builder_dispatch.BuilderDispatchError):
        builder_dispatch.held_card_ids(paths, now=NOW)


def test_a_demoted_node_still_holds_its_running_card(paths, operator, noded41) -> None:
    """Holding is keyed on the dispatch tree, not on the node's current role.

    A node taken out of the builder role, cordoned, or deleted from the
    registry mid-flight still has a worker on the card it claimed. Keying the
    answer on `_ready_builders()` would release that card to a local lane
    while the remote worker ran, which is the exact duplicate-work failure
    this change must not introduce.
    """
    _node(paths, operator, noded41)
    request = _offer(paths, RUNNING_CARD)
    builder_dispatch._write_status(paths, "node-ziowk01", request, "running")
    store.write_spec(
        paths,
        "node",
        "node-ziowk01",
        {"role": "worker-gpu", "actuate": False, "cordoned": True},
        writer=operator,
        labels={"host": "ziowk01"},
    )
    assert builder_dispatch._ready_builders(paths) == []
    assert builder_dispatch.held_card_ids(paths, now=NOW) == {RUNNING_CARD}


# --------------------------------------------------------------------------
# The rotation's withhold, lifted from the shipped script
# --------------------------------------------------------------------------


def test_the_rotation_keeps_a_candidate_the_builder_is_not_holding() -> None:
    """Before this fix every one of these rows was dropped from `owned`."""
    partition = _rotate_partition()
    rows = [_row(TERMINAL_CARD), _row(UNOFFERABLE_CARD), _row("24b00009"), _row("seat-card")]
    candidates = {TERMINAL_CARD, UNOFFERABLE_CARD, "24b00009"}
    kept, withheld, returned = partition(rows, candidates, set())
    assert withheld == []
    assert returned == [TERMINAL_CARD, UNOFFERABLE_CARD, "24b00009"]
    assert [row[2] for row in kept] == [
        TERMINAL_CARD,
        UNOFFERABLE_CARD,
        "24b00009",
        "seat-card",
    ]


def test_the_rotation_still_withholds_what_the_builder_holds() -> None:
    """Right of first refusal survives: a held candidate reaches no lane."""
    partition = _rotate_partition()
    rows = [_row(RUNNING_CARD), _row(TERMINAL_CARD), _row("seat-card")]
    candidates = {RUNNING_CARD, TERMINAL_CARD}
    kept, withheld, returned = partition(rows, candidates, {RUNNING_CARD})
    assert withheld == [RUNNING_CARD]
    assert returned == [TERMINAL_CARD]
    assert [row[2] for row in kept] == [TERMINAL_CARD, "seat-card"]


def test_the_rotation_never_withholds_a_non_candidate() -> None:
    """A held id that is not a builder candidate is not this rule's business."""
    partition = _rotate_partition()
    rows = [_row("seat-card")]
    kept, withheld, returned = partition(rows, set(), {"seat-card"})
    assert (withheld, returned) == ([], [])
    assert [row[2] for row in kept] == ["seat-card"]


def test_the_rotation_measured_tick_releases_the_parked_majority() -> None:
    """The measured chi tick, replayed: 16 candidates, 4 held, 12 released.

    chiap08 2026-09-19 01:54:55 CDT logged `builders-at-capacity:
    node-ziowk01=4/4` thirteen times, two `terminal:` refusals and one
    unofferable binding in a single tick, and then
    `SELECTION_EMPTY|reason=builder-path-withheld pool=19 owned=0`. Only the
    four dispatches node-ziowk01 was actually running were held by anything.
    """
    partition = _rotate_partition()
    running = [f"24b0000{number}" for number in range(1, 5)]
    parked = [TERMINAL_CARD, UNOFFERABLE_CARD] + [f"24c000{number:02d}" for number in range(10)]
    rows = [_row(card_id) for card_id in running + parked]
    candidates = set(running) | set(parked)
    kept, withheld, returned = partition(rows, candidates, set(running))
    assert len(withheld) == 4
    assert len(returned) == 12
    assert len(kept) == 12  # before: 0


def test_the_rotation_script_no_longer_withholds_on_eligibility_alone() -> None:
    """The PR #635 line is gone from the source that runs on every host."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "owned = [candidate for candidate in owned" not in source
    assert "builder_dispatch.held_card_ids(" in source
    assert "_builder_partition(" in source


def test_the_selection_diagnostic_reports_both_sides_of_the_split(tmp_path) -> None:
    """One grep shows whether an idle host is idle because of the builder."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_selection_diagnostic", "_bounded_ids"}
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert len(functions) == len(wanted)
    namespace: dict = {"collections": __import__("collections"), "re": re, "os": os, "json": json}
    exec(  # noqa: S102 - lifting the shipped source is the point of this test
        compile(ast.Module(body=functions, type_ignores=[]), str(ROTATE), "exec"),
        namespace,
    )
    pool = [_row(card_id) for card_id in (RUNNING_CARD, TERMINAL_CARD)]
    detail = namespace["_selection_diagnostic"](
        pool,
        [_row(TERMINAL_CARD)],
        [{"target": 5, "free": 5}],
        lambda _card_id: "chiap08",
        {"chiap08": 5},
        [RUNNING_CARD],
        [TERMINAL_CARD],
    )
    assert "builder_withheld=1" in detail
    assert "builder_returned=1" in detail
