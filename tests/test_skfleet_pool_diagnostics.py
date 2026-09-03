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


def test_missing_owner_capacity_is_unknown_not_zero() -> None:
    helpers = _load_helpers()
    pool = [_row("deadbeef")]

    detail = helpers["_selection_diagnostic"](
        pool, [], _lanes(target=4, free=4), lambda _card_id: "chiap04", {}
    )

    assert "owner_free=chiap04:unknown" in detail
    assert "owner_free=chiap04:0" not in detail


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
    assert "fresh_claimability=authoritative_claimability(cid,fresh=True)" in source
    assert "_current_claim_identity_fresh(cid)" in source
    assert "claimed_owner," in source
    assert "name)" in source
    assert "len(picks)<MAX_LAUNCH" in source
    assert "RACED|%s|count=%d ids=%s omitted=%d" in source
