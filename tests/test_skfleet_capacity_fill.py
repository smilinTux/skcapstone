"""Bounded fleet capacity-fill regressions for cards 0abd9b3c and 24b00002."""

from __future__ import annotations

import ast
import re
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


def _candidate_scan(owned, limit):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_GLM_SIZE_RE", "_LOGICAL_ROUTES"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted for target in node.targets
            )
        )
        or (
            isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "_size_class_for",
                "_logical_route_for",
                "_bounded_candidate_sequence",
            }
        )
        or (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "_candidate_scan"
                for target in node.targets
            )
        )
    ]
    namespace = {"re": re, "owned": owned, "MAX_CANDIDATE_SCAN": limit}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_candidate_scan"]


def _launchable_predicate():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_has_launchable_pick"
    )

    def select(labels, _escalation, _order, remaining, *_args):
        lane = "escalate" if "escalation-only" in labels else "codex"
        return (lane, "compatible") if remaining.get(lane, 0) else (None, "full")

    # qwen_suitable takes (core, labels) since gateway-routing work was kept
    # off the qwen lane. The stub must track its real arity or every caller
    # here fails with a TypeError that says nothing about capacity.
    namespace = {
        "qwen_suitable": lambda _core, _labels=None: True,
        "select_compatible_lane": select,
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_has_launchable_pick"]


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


def test_reconstructability_failures_do_not_starve_two_valid_cards() -> None:
    outcomes = [
        ("missing-review-metadata", False),
        ("missing-source-ref", False),
        ("workspace-verification-failed", False),
        ("workspace-materialization-failed", False),
        ("valid-a", True),
        ("valid-b", True),
    ]

    assert _fill(outcomes, seats=2, pool_bound=6) == ["valid-a", "valid-b"]


def test_reconstructability_scan_stops_at_numeric_ceiling() -> None:
    outcomes = [
        ("missing-review-metadata", False),
        ("missing-source-ref", False),
        ("valid-outside-ceiling", True),
    ]

    assert _fill(outcomes, seats=1, pool_bound=2) == []


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


def test_invalid_size_route_is_excluded_before_bounded_attempt_truncation() -> None:
    owned = [
        (0, 0, "conflicting", {"title": "[S][M] Conflict"}, [], 0),
        (1, 0, "valid", {"title": "[S] Valid"}, [], 0),
    ]

    assert [candidate[2] for candidate in _candidate_scan(owned, 1)] == ["valid"]


def test_runtime_counts_only_successful_launches() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert "(candidate for candidate in owned" in source
    assert "if _logical_route_for(candidate[3],candidate[4]) is not None)" in source
    assert "if not _has_launchable_pick(" in source
    assert "_attempt_lane_name,_attempt_defer=select_compatible_lane(" in source
    assert "_attempt_remaining," in source
    assert "else:\n        launched+=1" in source


def test_exhausted_elastic_budget_preserves_later_other_lane() -> None:
    predicate = _launchable_predicate()
    codex = ({"name": "codex"}, (0, 0, "codex-tail", {}, ["codex-only"], 0))
    escalation = (
        {"name": "escalate"},
        (0, 0, "escalation", {}, ["escalation-only"], 0),
    )
    admissions = {
        "codex-tail": (False, False, {"codex": (True, "healthy")}, True),
        "escalation": (True, False, {"escalate": (True, "healthy")}, False),
    }
    remaining = {"codex": 1, "escalate": 1}
    lane_order = ["codex", "escalate"]

    assert predicate([codex], remaining, 0, lane_order, admissions) is False
    assert predicate([codex, escalation], remaining, 0, lane_order, admissions) is True


def test_prelaunch_recheck_uses_gateway_routes_for_producer_health() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    recheck = source.index("_attempt_health={")
    selection = source.index("_attempt_lane_name,_attempt_defer=select_compatible_lane(", recheck)
    block = source[recheck:selection]

    assert "_producer_routes=_producer_routes_for(" in block
    assert '"codex" if "codex-only"' in block
    assert '_attempt_health["codex"]=(' in block
    assert 'bool(_producer_routes),"gateway-route-capacity"' in block


def test_first_pass_elastic_review_uses_codex_health_and_capacity_only() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    start = source.index("while _i<len(owned)")
    end = source.index("if _lane_deferred:", start)
    block = source[start:end]

    assert "_elastic_review = _POOL_V2_ADMISSIONS.get(_card[2], {}).get(" in block
    assert '_card_lane_health["codex"]=(' in block
    assert 'remaining.get("codex",0)>0,"review-route-capacity"' in block
    assert "if _elastic_review else remaining" in block


def test_elastic_review_limit_counts_successes_not_preflight_candidates() -> None:
    source = ROTATE.read_text(encoding="utf-8")

    assert "_elastic_rows[:_elastic_limit]" not in source
    assert "elastic_launch_remaining = _elastic_limit" in source
    assert source.count('min(remaining.get("codex", 0), elastic_launch_remaining)') == 1
    assert source.count("elastic_launch_remaining-=1") == 2

    workspace_block = source.index('log(d,"WORKSPACE_BLOCKED|')
    successful_launch = source.index("elastic_launch_remaining-=1", workspace_block)
    assert successful_launch > source.index("else:\n        launched+=1", workspace_block)
