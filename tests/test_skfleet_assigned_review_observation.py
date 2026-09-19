"""Regressions for bounded post-selection review observation."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
ROTATE = ROOT / "scripts/fleet/skfleet-rotate.py"


def _observer(namespace: dict[str, object]):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_observe_assigned_reviews"
    )
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_observe_assigned_reviews"]


def test_empty_selection_observes_only_immutable_review_snapshot_over_7000_cards(
    tmp_path: Path,
) -> None:
    """Irrelevant CardStore population cannot delay the terminal no-op path."""

    cards = tmp_path / "cards"
    cards.mkdir()
    for index in range(7_001):
        (cards / f"card-{index:04d}").mkdir()

    observed: list[str] = []
    reconciled: list[str] = []
    review_id = "deadbeef"
    namespace = {
        "_POOL_V2_ADMISSIONS": {
            review_id: {"labels": ["review", "seat-seraph"]},
            "feedface": {"labels": ["source-only"]},
        },
        "sh": lambda *_args: "",
        "active_worker_units": lambda: set(),
        "_load_outcomes": lambda: {},
        "event_rows": lambda cid: observed.append(cid) or [],
        "reconcile_fanout_receipt": lambda _home, cid, **_kwargs: reconciled.append(cid),
        "Path": Path,
        "HOME": str(tmp_path),
        "HOST": "chiap08",
        "d": str(tmp_path / "evidence"),
        "log": lambda *_args: None,
        "FanoutBoundaryError": ValueError,
        "BoundaryError": ValueError,
        "OSError": OSError,
        "ValueError": ValueError,
    }

    _observer(namespace)()

    assert observed == [review_id]
    assert reconciled == [review_id]


def test_empty_selection_has_exactly_one_typed_terminal_receipt() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    block = source[source.index("if not picks:") : source.index("raced=0;")]

    assert block.count('log(d,"NOOP_RECEIPT|%s|reason=%s|seat=%s"%') == 1
