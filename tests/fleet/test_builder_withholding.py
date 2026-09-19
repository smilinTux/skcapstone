"""Only a card the builder will never take is released to the local lanes.

Every builder candidate used to be withheld from its owning host's lanes
unconditionally, so a host whose whole hash slice was builder work selected
nothing at all. Measured on the chi estate 2026-09-19, five hosts at
``owned=0`` with 65 free seats between them.

The release is the one direction that can put a local lane and a remote
builder on the same card, so these tests pin the safety property from both
ends: a released reason must be one ``offer()`` refuses to act on, and a
capacity reason must never be released.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import builder_dispatch, sknoded, store

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "src" / "skcapstone" / "fleet" / "builder_dispatch.py"

# Every reason decline_reason() can answer, classified. A release is safe only
# when offer() cannot write a request for the card, so the third column is the
# claim each entry makes and test_every_reason_is_classified keeps it complete.
REASON_VOCABULARY = {
    # reason prefix: (durable, why)
    "actuation-frozen": (False, "an operator thaws the plane without the owner seeing it"),
    "ineligible": (False, "unreachable: the candidate set is already eligible()"),
    "invalid-card-id": (True, "offer() raises before any write; card ids are immutable"),
    "invalid-source:": (True, "offer() raises before any write"),
    "no-ready-builder": (False, "a builder joining makes the WHOLE slice offerable at once"),
    "terminal:": (True, "same-binding terminal status, budget spent, no new generation"),
    "parked:": (True, "#802's parked() is monotone; unworked terminals are forgiven instead"),
    "superseded-binding-running:": (False, "the run ends and the card is offered again"),
    "builders-at-capacity:": (False, "a slot drains and the next cycle offers the card"),
    "unschedulable:": (False, "the scheduler's answer changes with fleet state"),
}


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


def _card(card_id: str = "24b00003") -> dict:
    return {
        "id": card_id,
        "meta": {
            "repository": "https://github.com/smilinTux/skcapstone.git",
            "base_ref": "main",
            "base_revision": "9cc415465d6bacc22b51b09a3c861c61f0823d45",
        },
    }


@pytest.fixture(autouse=True)
def _folded_card(monkeypatch):
    monkeypatch.setattr(
        builder_dispatch.CardStore,
        "fold",
        lambda *_args: SimpleNamespace(
            id="24b00003",
            owner=None,
            meta=dict(_card()["meta"]),
            labels=["sk-m", "source-only"],
            status=SimpleNamespace(value="doing"),
            links={},
        ),
    )


def _writer():
    return store.Writer(role="scheduler", node="niobe", identity="capauth:niobe")


def _decline_reasons_in_source() -> set[str]:
    """Read every literal reason decline_reason() can return, from its AST."""
    tree = ast.parse(DISPATCH.read_text(encoding="utf-8"))
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "decline_reason"
    )
    found = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            found.add(value.value)
        elif isinstance(value, ast.JoinedStr):
            head = value.values[0]
            if isinstance(head, ast.Constant):
                found.add(str(head.value).split("{")[0])
    return found


def test_every_reason_is_classified() -> None:
    """A new decline reason must be classified before it can be released.

    Without this, adding a reason silently defaults it to withheld (safe) or,
    worse, to a prefix that already matches a durable entry (a race). The
    vocabulary is read out of decline_reason()'s own AST so the table cannot
    drift from the function.
    """
    for reason in _decline_reasons_in_source():
        assert any(
            reason.startswith(prefix) or prefix.startswith(reason) for prefix in REASON_VOCABULARY
        ), f"unclassified decline reason: {reason!r}"


def test_classification_matches_the_table() -> None:
    for prefix, (durable, why) in REASON_VOCABULARY.items():
        assert builder_dispatch.durable_decline(prefix + " detail") is durable, why


def test_offerable_is_never_released() -> None:
    assert builder_dispatch.durable_decline(None) is False
    assert builder_dispatch.durable_decline("") is False


def test_a_parked_card_is_released_to_the_local_lane(paths, operator, noded41) -> None:
    """The release the whole change exists for: a card no builder will take."""
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=_writer())
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "failed",
        attempt=builder_dispatch.MAX_ATTEMPTS,
        completion={"verdict": "FAIL"},
    )
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert builder_dispatch.durable_decline(reason)
    withheld, released = builder_dispatch.partition_withheld(["24b00003"], lambda _cid: reason)
    assert withheld == []
    assert released == [("24b00003", reason)]


def test_a_released_card_cannot_also_be_offered(paths, operator, noded41) -> None:
    """The safety property, end to end: release implies offer() writes nothing.

    This is the test that fails if the durable set ever grows to include a
    capacity reason. It does not trust the classification table; it asks
    offer() itself, in the same fleet state decline_reason() just read.
    """
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=_writer())
    path = builder_dispatch.request_path(paths, "node-ziowk01", "24b00003")
    builder_dispatch._write_status(
        paths,
        "node-ziowk01",
        request,
        "failed",
        attempt=builder_dispatch.MAX_ATTEMPTS,
        completion={"verdict": "FAIL"},
    )
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert builder_dispatch.durable_decline(reason), reason
    before = path.read_bytes()
    assert (
        builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=_writer()) is None
    )
    assert path.read_bytes() == before, "a released card was re-offered to the builder"


@pytest.mark.parametrize(
    "status",
    [
        {"state": "blocked", "attempt": 0, "error": "unclaimed offer expired"},
        {"state": "blocked", "attempt": 0, "error": "offered card changed"},
        {"state": "failed", "attempt": 1},
        {"state": "failed", "attempt": builder_dispatch.MAX_ATTEMPTS},
        {
            "state": "failed",
            "attempt": builder_dispatch.MAX_ATTEMPTS,
            "completion": {"verdict": "FAIL"},
        },
        {"state": "stale", "attempt": 1},
        {"state": "completed", "attempt": 1, "completion": {"verdict": "PASS"}},
        {"state": "running", "attempt": 1},
    ],
)
def test_release_implies_offer_writes_nothing(paths, operator, noded41, status) -> None:
    """The safety property, asserted against offer() rather than a table.

    Every terminal and nonterminal dispatch status the chi board actually
    carries, including the four shapes #802 measured on node-ziowk01. For each
    one: if the card is released, offer() must leave the dispatch tree exactly
    as it was. This is deliberately version-independent. #802 forgives unworked
    terminals with a fresh generation, which moves several of these rows OUT of
    the released set; the assertion still holds either way, because it asks
    offer() in the same state decline_reason() just read instead of assuming
    which rows are durable.
    """
    _node(paths, operator, noded41)
    request = builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=_writer())
    builder_dispatch._write_status(paths, "node-ziowk01", request, **status)
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    if not builder_dispatch.durable_decline(reason):
        pytest.skip(f"withheld, not released: {reason}")
    before = _dispatch_tree(paths)
    assert (
        builder_dispatch.offer(paths, _card(), ["sk-m", "source-only"], writer=_writer()) is None
    )
    assert _dispatch_tree(paths) == before, f"released card re-offered under {reason!r}"


def _dispatch_tree(paths) -> dict[str, bytes]:
    """Every request the Niobe path has written, as raw bytes."""
    root = paths.root / "dispatch"
    return {
        str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob("*.json"))
    }


def test_an_invalid_card_id_is_released_and_never_written(paths, operator, noded41) -> None:
    """offer() raises before it writes, so no builder can ever claim this."""
    _node(paths, operator, noded41)
    bad = _card("Not A Card Id")
    reason = builder_dispatch.decline_reason(paths, bad, ["sk-m", "source-only"])
    assert reason == "invalid-card-id"
    assert builder_dispatch.durable_decline(reason)
    with pytest.raises(builder_dispatch.BuilderDispatchError):
        builder_dispatch.offer(paths, bad, ["sk-m", "source-only"], writer=_writer())
    assert not (paths.root / "dispatch" / "node-ziowk01" / "Not A Card Id.json").exists()


def test_capacity_is_withheld_not_released(paths, operator, noded41) -> None:
    """The class that idled 14 seats, and the one that must stay withheld.

    Measured on chi 2026-09-19: 14 of 21 pool cards declined with
    ``builders-at-capacity: node-ziowk01=4/4``. Releasing them is what would
    force the number up, and it is exactly the double claim the withholding
    exists to prevent: the slot drains, the Niobe host offers the card, and
    the owning host has already handed it to a lane.
    """
    _node(paths, operator, noded41)
    for index in range(builder_dispatch.BUILDER_CAPACITY):
        card = _card(f"24b0100{index}")
        builder_dispatch.offer(paths, card, ["sk-m", "source-only"], writer=_writer())
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason.startswith("builders-at-capacity:"), reason
    withheld, released = builder_dispatch.partition_withheld(["24b00003"], lambda _cid: reason)
    assert withheld == ["24b00003"]
    assert released == []


def test_no_ready_builder_is_withheld_not_released(paths) -> None:
    """Rejects the narrow proposal that called this durable.

    A builder joining is a capacity event. When it arrives every candidate
    becomes offerable in the same cycle, so a release here races the host's
    entire slice at once rather than one card.
    """
    reason = builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"])
    assert reason == "no-ready-builder"
    assert builder_dispatch.durable_decline(reason) is False


def test_a_failed_lookup_is_withheld() -> None:
    """Fail closed: an unreadable fleet tree must not release anything."""

    def explode(_card_id):
        raise OSError("fleet tree unreadable")

    withheld, released = builder_dispatch.partition_withheld(["24b00003"], explode)
    assert withheld == ["24b00003"]
    assert released == []


def test_partition_preserves_input_order() -> None:
    reasons = {
        "aaaaaaaa": "builders-at-capacity: node-ziowk01=4/4",
        "bbbbbbbb": "terminal: node=node-ziowk01 state=failed attempt=2",
        "cccccccc": None,
        "dddddddd": "invalid-card-id",
    }
    withheld, released = builder_dispatch.partition_withheld(
        list(reasons), lambda cid: reasons[cid]
    )
    assert withheld == ["aaaaaaaa", "cccccccc"]
    assert [cid for cid, _reason in released] == ["bbbbbbbb", "dddddddd"]


def test_offer_and_decline_agree_on_a_fresh_card(paths, operator, noded41) -> None:
    """Nothing is released while the builder would still place the card."""
    _node(paths, operator, noded41)
    assert builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"]) is None
    withheld, released = builder_dispatch.partition_withheld(
        ["24b00003"],
        lambda _cid: builder_dispatch.decline_reason(paths, _card(), ["sk-m", "source-only"]),
    )
    assert withheld == ["24b00003"]
    assert released == []
    assert (
        builder_dispatch.offer(
            paths,
            _card(),
            ["sk-m", "source-only"],
            writer=_writer(),
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        is not None
    )
