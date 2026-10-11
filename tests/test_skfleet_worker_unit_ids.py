"""Regression coverage for bounded reversible fleet worker unit identities.

Card ``9e70236b``. The visible-wave fan-out crashed in ``_worker_unit_name``
when an admitted canonical CardStore ID contained a suffix such as
``a8100002-1``. These tests pin the bounded, reversible encoding:

* legacy eight-hex IDs keep their historical literal service names;
* suffixed and otherwise bounded canonical IDs produce one unique unit each;
* the parser recovers the exact original card ID and lane with no collision or
  ambiguous truncation;
* malformed or overlong IDs fail closed for exactly one card and never abort a
  multi-card cycle.

Run: python3 -m pytest tests/test_skfleet_worker_unit_ids.py
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

LANES = ("codex", "glm", "qwen", "kimi", "escalate")


def _helpers(*names: str) -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = tree.body
    dependencies = {"_worker_unit_name": {"_worker_card_token"}}
    wanted = set(names)
    for name in list(names):
        wanted |= dependencies.get(name, set())
    nodes = {
        node.name: node
        for node in body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    }
    assert wanted <= set(nodes), f"missing functions: {wanted - set(nodes)}"
    constants = {
        "_WORKER_UNIT_RE",
        "_WORKER_CARD_RE",
        "_WORKER_CARD_RE_HEX8",
        "_WORKER_CARD_MAX",
        "_WORKER_LANES",
    }
    assignments = [
        node
        for node in body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets)
    ]
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(assignments + [nodes[name] for name in sorted(wanted)], [])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    "card_id",
    [
        "deadbeef",
        "a8100002-1",
        "a8100005-c1a15a",
        "inc-0e190b2f",
        "a8100007-03",
    ],
)
def test_worker_unit_round_trips_canonical_card_ids(card_id: str) -> None:
    helpers = _helpers("_worker_unit_name", "_parse_worker_units")
    unit = helpers["_worker_unit_name"]("codex", card_id)
    parsed = helpers["_parse_worker_units"](f"{unit} loaded active running worker")

    assert parsed == [{"unit": unit, "lane": "codex", "card": card_id}]


def test_legacy_eight_hex_id_keeps_literal_unit_name() -> None:
    make = _helpers("_worker_unit_name")["_worker_unit_name"]

    assert make("codex", "3b227de2") == "skfleet-worker-codex-3b227de2.service"
    assert make("glm", "feedface") == "skfleet-worker-glm-feedface.service"


@pytest.mark.parametrize("lane", LANES)
def test_round_trip_is_lane_exact(lane: str) -> None:
    helpers = _helpers("_worker_unit_name", "_parse_worker_units")
    unit = helpers["_worker_unit_name"](lane, "a8100002-1")
    parsed = helpers["_parse_worker_units"](unit)

    assert parsed == [{"unit": unit, "lane": lane, "card": "a8100002-1"}]


def test_suffixed_and_legacy_ids_never_collide() -> None:
    make = _helpers("_worker_unit_name")["_worker_unit_name"]

    units = {
        make("codex", card_id) for card_id in ("a8100002", "a8100002-1", "a8100002-2", "deadbeef")
    }
    assert len(units) == 4


@pytest.mark.parametrize(
    "card_id",
    [
        "SHORT",
        "ABCDEF12",
        "../../bad",
        "5a71c2dd-",
        "-5a71c2dd",
        "5a71--c2dd",
        "a" * 65,
        "",
    ],
)
def test_malformed_or_overlong_ids_fail_closed(card_id: str) -> None:
    make = _helpers("_worker_unit_name")["_worker_unit_name"]

    with pytest.raises(ValueError, match="invalid worker unit identity"):
        make("codex", card_id)


def test_malformed_id_does_not_block_valid_subsequent_card() -> None:
    make = _helpers("_worker_unit_name")["_worker_unit_name"]
    launched = []
    for card_id in ("a8100002-1", "../../bad", "5a71c2dd"):
        try:
            unit = make("codex", card_id)
        except ValueError:
            continue
        launched.append((card_id, unit))

    assert launched == [
        ("a8100002-1", "skfleet-worker-codex-a8100002-1.service"),
        ("5a71c2dd", "skfleet-worker-codex-5a71c2dd.service"),
    ]


def test_parser_ignores_units_that_do_not_match_bounded_grammar() -> None:
    parse = _helpers("_parse_worker_units")["_parse_worker_units"]
    output = "\n".join(
        [
            "skfleet-worker-codex-a8100002-1.service loaded active running worker",
            "skfleet-worker-codex-.service loaded active running worker",
            "skfleet-worker-codex-5a71--c2dd.service loaded active running worker",
            "skfleet-worker-bad-deadbeef.service loaded active running worker",
            "unrelated.service loaded active running other",
        ]
    )

    assert parse(output) == [
        {
            "unit": "skfleet-worker-codex-a8100002-1.service",
            "lane": "codex",
            "card": "a8100002-1",
        }
    ]
