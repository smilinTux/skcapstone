"""Pure selection tests for the operational fleet rotation script."""

import ast
from collections import Counter
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_selector():
    """Load only the pure helper without executing the operational script."""
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"), filename=str(SCRIPT))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_select_picks"
    )
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["_select_picks"]


SELECT = _load_selector()


def _card(index, escalates=False):
    """Build the minimal production-shaped card row used by the selector."""
    return [0, 0, f"card-{index}", {"escalates": escalates}, 0]


def _lanes(codex=8, glm=3, escalate=2):
    """Build free lane capacities without any live scheduler state."""
    return [
        {"name": "codex", "free": codex},
        {"name": "glm", "free": glm},
        {"name": "escalate", "free": escalate},
    ]


def _select(cards, limit, **capacity):
    """Run the selector with a deterministic synthetic classifier."""
    return SELECT(cards, _lanes(**capacity), limit, lambda _cid, core: core["escalates"])


@pytest.mark.parametrize("size", range(1, 16))
@pytest.mark.parametrize("limit", (2, 8, 11))
def test_codex_gets_one_bounded_opportunity(size, limit):
    cards = [_card(index) for index in range(size)]

    picks, waiting = _select(cards, limit)
    counts = Counter(lane["name"] for lane, _card_row in picks)

    assert picks[0][0]["name"] == "codex"
    assert picks[0][1] is cards[0]
    assert counts["codex"] >= 1
    assert counts["codex"] <= 8
    assert counts["glm"] <= 3
    assert counts["escalate"] == 0
    assert len(picks) <= min(size, limit)
    assert waiting == 0


def test_remaining_ordinary_work_stays_glm_first():
    cards = [_card(index) for index in range(4)]

    picks, waiting = _select(cards, 4)

    assert [lane["name"] for lane, _card_row in picks] == ["codex", "glm", "glm", "glm"]
    assert [card for _lane, card in picks] == cards
    assert waiting == 0


def test_first_ordinary_card_is_reserved_ahead_of_escalation():
    escalation = _card(0, escalates=True)
    ordinary = _card(1)

    picks, waiting = _select([escalation, ordinary], 1)

    assert picks == [(_lanes()[0], ordinary)]
    assert waiting == 0


def test_mixed_cards_keep_exact_affinity():
    cards = [_card(0), _card(1, True), _card(2), _card(3, True), _card(4)]

    picks, waiting = _select(cards, 5)
    routed = {card[2]: lane["name"] for lane, card in picks}

    assert routed == {
        "card-0": "codex",
        "card-1": "escalate",
        "card-2": "glm",
        "card-3": "escalate",
        "card-4": "glm",
    }
    assert waiting == 0


def test_full_escalation_lane_skips_and_continues():
    escalation = _card(0, escalates=True)
    ordinary = _card(1)

    picks, waiting = _select([escalation, ordinary], 2, escalate=0)

    assert picks == [(_lanes(escalate=0)[0], ordinary)]
    assert waiting == 1


def test_escalation_cards_never_fall_back_to_ordinary_lanes():
    cards = [_card(index, escalates=True) for index in range(3)]

    picks, waiting = _select(cards, 11)

    assert [lane["name"] for lane, _card_row in picks] == ["escalate", "escalate"]
    assert waiting == 1


def test_zero_codex_capacity_disables_only_the_reservation():
    cards = [_card(index) for index in range(5)]

    picks, waiting = _select(cards, 11, codex=0)

    assert [lane["name"] for lane, _card_row in picks] == ["glm", "glm", "glm"]
    assert [card for _lane, card in picks] == cards[:3]
    assert waiting == 0


def test_zero_glm_capacity_uses_only_codex_for_ordinary_work():
    cards = [_card(index) for index in range(10)]

    picks, waiting = _select(cards, 11, glm=0)

    assert [lane["name"] for lane, _card_row in picks] == ["codex"] * 8
    assert waiting == 0


def test_zero_launch_limit_returns_no_picks():
    picks, waiting = _select([_card(0)], 0)

    assert picks == []
    assert waiting == 0
