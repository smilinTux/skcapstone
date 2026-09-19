"""Deterministic diagnostics for empty local fleet selections."""

from __future__ import annotations

import ast
import collections
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
HOSTS = ("chiap01", "chiap02", "chiap03", "chiap04", "chiap08")


def _load_helpers() -> dict[str, object]:
    names = {"_bounded_ids", "_partition_owner", "_selection_diagnostic"}
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace: dict[str, object] = {
        "collections": collections,
        "hashlib": hashlib,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert names <= namespace.keys()
    return namespace


def _row(card_id: str) -> list[object]:
    return [1, 0, card_id, {}, [], 0]


def _lanes(*, target: int, free: int) -> list[dict[str, int | str]]:
    return [{"name": "codex", "target": target, "free": free}]


def test_pool_with_free_capacity_and_empty_local_partition_is_truthful() -> None:
    helpers = _load_helpers()
    local = "chiap04"
    foreign_ids = []
    candidate = 0
    while len(foreign_ids) < 4:
        card_id = f"{candidate:08x}"
        if helpers["_partition_owner"](card_id, HOSTS) != local:
            foreign_ids.append(card_id)
        candidate += 1
    pool = [_row(card_id) for card_id in foreign_ids]

    detail = helpers["_selection_diagnostic"](
        pool,
        [],
        _lanes(target=4, free=4),
        lambda card_id: helpers["_partition_owner"](card_id, HOSTS),
        {host: 1 for host in HOSTS},
    )

    assert "reason=foreign-hash-partition" in detail
    assert "pool=4 owned=0 target=4 free=4" in detail
    assert "no dependency-clear cards" not in detail
    assert "owner_free=" in detail


def test_builder_withheld_slice_is_not_reported_as_foreign() -> None:
    """A slice handed to the builder path must not read as a foreign partition.

    Regression for the 2026-09-18 chi fleet logs: a host whose entire hash
    slice was source-only builder work logged the self-contradicting line
    ``owned=0 ... owners=<this host>:N`` under ``foreign-hash-partition``.
    """
    helpers = _load_helpers()
    local = "chiap03"
    owned_ids = []
    candidate = 0
    while len(owned_ids) < 3:
        card_id = f"{candidate:08x}"
        if helpers["_partition_owner"](card_id, HOSTS) == local:
            owned_ids.append(card_id)
        candidate += 1
    pool = [_row(card_id) for card_id in owned_ids]

    detail = helpers["_selection_diagnostic"](
        pool,
        [],
        _lanes(target=4, free=4),
        lambda card_id: helpers["_partition_owner"](card_id, HOSTS),
        {host: 1 for host in HOSTS},
        owned_ids,
    )

    assert "reason=builder-path-withheld" in detail
    assert "builder_withheld=3" in detail
    assert "foreign-hash-partition" not in detail
    assert "ids=" + ",".join(sorted(owned_ids)) in detail


def test_builder_withheld_count_is_always_reported() -> None:
    helpers = _load_helpers()
    detail = helpers["_selection_diagnostic"](
        [_row("a")], [], _lanes(target=2, free=2), lambda _card_id: "chiap01"
    )
    assert "reason=foreign-hash-partition" in detail
    assert "builder_withheld=0" in detail


@pytest.mark.parametrize(
    ("capacity", "expected", "excluded"),
    [
        ({}, "owner_free=chiap04:unknown", "owner_free=chiap04:0"),
        ({"chiap04": 0}, "owner_free=chiap04:0", "owner_free=chiap04:unknown"),
    ],
    ids=("missing-is-unknown", "reported-zero-remains-zero"),
)
def test_owner_capacity_diagnostic_distinguishes_absent_from_zero(
    capacity: dict[str, int], expected: str, excluded: str
) -> None:
    helpers = _load_helpers()
    detail = helpers["_selection_diagnostic"](
        [_row("deadbeef")],
        [],
        _lanes(target=4, free=4),
        lambda _card_id: "chiap04",
        capacity,
    )

    assert expected in detail
    assert excluded not in detail


@pytest.mark.parametrize(
    ("pool", "owned", "lanes", "expected"),
    [
        ([], [], _lanes(target=4, free=4), "reason=empty-pool"),
        ([_row("a")], [_row("a")], _lanes(target=0, free=0), "reason=zero-target"),
        ([_row("a")], [_row("a")], _lanes(target=2, free=2), "reason=no-compatible-lane"),
    ],
)
def test_empty_selection_classes_are_separate(
    pool: list[list[object]],
    owned: list[list[object]],
    lanes: list[dict[str, int | str]],
    expected: str,
) -> None:
    helpers = _load_helpers()
    detail = helpers["_selection_diagnostic"](pool, owned, lanes, lambda _card_id: "chiap01")
    assert expected in detail


def test_diagnostic_ids_are_exact_counted_and_bounded() -> None:
    helpers = _load_helpers()
    pool = [_row(f"card-{index:02d}") for index in range(20)]
    detail = helpers["_selection_diagnostic"](
        pool, [], _lanes(target=1, free=1), lambda _card_id: "chiap01"
    )
    assert "pool=20 owned=0" in detail
    assert "ids=" + ",".join(f"card-{index:02d}" for index in range(12)) in detail
    assert "omitted=8" in detail
    assert "card-12" not in detail


def test_partition_owner_is_unique_and_pins_override_hash() -> None:
    helpers = _load_helpers()
    card_id = "eligible-card"
    owner = helpers["_partition_owner"](card_id, HOSTS)
    assert owner in HOSTS
    assert sum(host == owner for host in HOSTS) == 1
    assert helpers["_partition_owner"](card_id, HOSTS, "chiap08") == "chiap08"


def test_source_preserves_admission_and_reports_selection_races() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert 'reason, ids = "foreign-hash-partition", pool_ids' in source
    assert "reason=%s pool=%d owned=%d target=%d free=%d" in source
    assert "SELECTION_EMPTY|" in source
    assert "no dependency-clear cards" not in source
    fresh_core = source.index(
        'with open(os.path.join(CARDS,cid,"core.json"),encoding="utf-8") as _handle:'
    )
    fresh_claimability = source.index(
        "fresh_claimability=authoritative_claimability(cid,core=_fresh_core,fresh=True)"
    )
    assert fresh_core < fresh_claimability
    assert "_current_claim_identity_fresh(cid)" in source
    assert "claimed_owner," in source
    assert "name)" in source
    assert "(candidate for candidate in owned" in source
    assert "if _logical_route_for(candidate[3],candidate[4]) is not None)" in source
    assert "launched>=MAX_LAUNCH" in source
    assert "RACED|%s|count=%d ids=%s omitted=%d" in source


def test_a_card_that_never_reached_lane_selection_is_not_a_lane_verdict() -> None:
    """``no-compatible-lane`` must not absorb the unroutable case.

    A candidate with no logical route is filtered out of the candidate scan
    before any lane is consulted, so reporting a lane verdict for it is a false
    statement about where it died. On chi 2026-09-19 every host logged
    ``reason=no-compatible-lane`` while its whole owned slice had been dropped
    for a missing size, and three consecutive diagnoses chased lane health,
    lane targets and claim ceilings on the strength of that string.
    """
    helpers = _load_helpers()
    owned_ids = ["a728f287", "c33a1004", "cf460fde"]
    pool = [_row(card_id) for card_id in owned_ids]
    owned = list(pool)

    detail = helpers["_selection_diagnostic"](
        pool,
        owned,
        _lanes(target=9, free=9),
        lambda _card_id: "chiap04",
        None,
        (),
        (),
        owned_ids,
    )
    assert "reason=unroutable-size" in detail
    assert "ids=a728f287,c33a1004,cf460fde" in detail

    # A lane verdict is still a lane verdict when the cards DID reach selection.
    detail = helpers["_selection_diagnostic"](
        pool,
        owned,
        _lanes(target=9, free=9),
        lambda _card_id: "chiap04",
    )
    assert "reason=no-compatible-lane" in detail

    # A partial drop is a lane verdict too: something did reach selection.
    detail = helpers["_selection_diagnostic"](
        pool,
        owned,
        _lanes(target=9, free=9),
        lambda _card_id: "chiap04",
        None,
        (),
        (),
        owned_ids[:1],
    )
    assert "reason=no-compatible-lane" in detail


def test_the_unrouted_drop_is_logged_before_the_scan_consumes_it() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "UNROUTED_CANDIDATES|%s|count=%d|reason=missing-or-ambiguous-size|" in source
    drop = source.index("_unrouted_candidates=[candidate[2] for candidate in owned")
    scan = source.index("_candidate_scan = _bounded_candidate_sequence(")
    assert drop < scan
    assert "_builder_returned_ids, _unrouted_candidates)" in source
