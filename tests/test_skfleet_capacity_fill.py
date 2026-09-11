"""Bounded fleet capacity-fill regressions for card 0abd9b3c."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts/fleet/skfleet-rotate.py"


def _bounded_sequence():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_bounded_candidate_sequence"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_bounded_candidate_sequence"]


def _fill(outcomes: list[tuple[str, bool]], seats: int, pool_bound: int) -> list[str]:
    candidates = [(0, 0, card_id) for card_id, _ok in outcomes]
    bounded = _bounded_sequence()(candidates, pool_bound)
    result: list[str] = []
    outcome = dict(outcomes)
    for candidate in bounded:
        if len(result) >= seats:
            break
        if outcome[candidate[2]]:
            result.append(candidate[2])
    return result


def test_early_failures_do_not_consume_success_slots() -> None:
    outcomes = [("workspace-fail", False), ("admission-fail", False), ("later", True)]
    assert _fill(outcomes, 1, 3) == ["later"]


def test_multiple_slots_fill_after_early_failures() -> None:
    outcomes = [
        ("workspace-fail", False),
        ("later-a", True),
        ("claim-race", False),
        ("later-b", True),
    ]
    assert _fill(outcomes, 2, 4) == ["later-a", "later-b"]


@pytest.mark.parametrize(
    ("outcomes", "seats", "bound", "expected"),
    [
        ([("a", False), ("b", False)], 2, 2, []),
        ([("a", True), ("b", True)], 1, 2, ["a"]),
        ([("a", False), ("b", True), ("c", True)], 2, 2, ["b"]),
        ([("blocked", False), ("eligible", True)], 0, 2, []),
    ],
)
def test_pool_bound_all_fail_and_capacity_full(outcomes, seats, bound, expected) -> None:
    assert _fill(outcomes, seats, bound) == expected


def test_duplicate_candidates_are_attempted_once_in_order() -> None:
    bounded = _bounded_sequence()([(0, 0, "a"), (1, 0, "a"), (2, 0, "b"), (3, 0, "c")], 3)
    assert [candidate[2] for candidate in bounded] == ["a", "b", "c"]


def test_runtime_counts_only_successful_launches() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "_bounded_candidate_sequence(owned, MAX_CANDIDATE_SCAN)" in source
    assert "if launched>=MAX_LAUNCH or not any(launch_remaining.values()):" in source
    assert "_attempt_lane_name,_attempt_defer=select_compatible_lane(" in source
    assert "_attempt_remaining," in source
    assert "else:\n        launched+=1" in source


def test_prelaunch_recheck_uses_gateway_routes_for_producer_health() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    recheck = source.index("_attempt_health={")
    selection = source.index("_attempt_lane_name,_attempt_defer=select_compatible_lane(", recheck)
    block = source[recheck:selection]

    assert "_producer_routes=_producer_routes_for(" in block
    assert '"codex" if "codex-only"' in block
    assert '_attempt_health["codex"]=(' in block
    assert 'bool(_producer_routes),"gateway-route-capacity"' in block
