"""Regression coverage for reversible fleet worker unit identities."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_worker_unit_name", "_parse_worker_units"}
    nodes = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    }
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_WORKER_UNIT_RE"
            for target in node.targets
        )
    ]
    assert set(nodes) == wanted
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(assignments + list(nodes.values()), [])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize("card_id", ["deadbeef", "a8100002-1", "a8100005-c1a15a"])
def test_worker_unit_round_trips_canonical_card_ids(card_id: str) -> None:
    helpers = _helpers()
    unit = helpers["_worker_unit_name"]("codex", card_id)
    parsed = helpers["_parse_worker_units"](f"{unit} loaded active running worker")

    assert parsed == [{"unit": unit, "lane": "codex", "card": card_id}]


@pytest.mark.parametrize(
    "card_id",
    ["short", "UPPER123", "bad--id1", "-badcard", "badcard-", "a" * 65],
)
def test_worker_unit_rejects_malformed_card_ids(card_id: str) -> None:
    helpers = _helpers()

    with pytest.raises(ValueError, match="invalid worker unit identity"):
        helpers["_worker_unit_name"]("codex", card_id)


def test_parser_ignores_malformed_units_and_keeps_following_valid_unit() -> None:
    parse = _helpers()["_parse_worker_units"]
    output = "\n".join(
        [
            "skfleet-worker-codex-bad--id1.service loaded active running bad",
            "skfleet-worker-codex-a8100007-01.service loaded active running good",
        ]
    )

    assert parse(output) == [
        {
            "unit": "skfleet-worker-codex-a8100007-01.service",
            "lane": "codex",
            "card": "a8100007-01",
        }
    ]


def test_scheduler_catches_identity_failure_before_claim() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    validation = source.index('unit=_worker_unit_name(_LANE["name"],cid)')
    claim = source.index('claim=subprocess.run([SKC,"coord","claim",cid')

    assert validation < claim
    assert "WORKER_ID_BLOCKED" in source[validation:claim]
