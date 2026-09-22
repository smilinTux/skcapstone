"""Regressions for bounded post-selection review observation."""

from __future__ import annotations

import ast
import json
import re
import subprocess
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
        "_claim_rows": {},
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
    assert reconciled == []


def test_empty_selection_has_exactly_one_typed_terminal_receipt() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    block = source[source.index("if not picks:") : source.index("raced=0;")]

    assert block.count('log(d,"NOOP_RECEIPT|%s|reason=%s|seat=%s"%') == 1


def test_4612_card_observer_reuses_pool_fold_within_child_budget(tmp_path: Path) -> None:
    """The live 1,565-review tail must not repeat 313 seconds of card probes."""

    total_cards = 4_612
    review_cards = 1_565
    review_ids = [f"{index:08x}" for index in range(review_cards)]
    assigned_id = review_ids[-1]
    fanout_id = review_ids[-2]
    admissions = {
        card_id: {"labels": ["review", "seat-seraph"] if index < review_cards else ["source-only"]}
        for index, card_id in enumerate(f"{index:08x}" for index in range(total_cards))
    }
    cached_rows = {card_id: [] for card_id in review_ids}
    cached_rows[fanout_id] = [{"action": "niobe_fanout_request"}]
    cached_rows[assigned_id] = [
        {"action": "review_assignment_launch", "claim_revision": "revision-1"},
        {
            "action": "mero_observation",
            "process": {"host": "chiap08", "session": "review-session"},
        },
    ]
    simulated_probe_seconds = 0.0

    def repeated_event_probe(card_id: str) -> list[dict[str, object]]:
        """Model the measured per-card fold/probe cost without sleeping."""

        nonlocal simulated_probe_seconds
        simulated_probe_seconds += 0.2
        if simulated_probe_seconds > 270:
            raise subprocess.TimeoutExpired("niobe-child", 270)
        return cached_rows[card_id]

    reconciled: list[str] = []
    observed: list[dict[str, object]] = []

    class Observation:
        """Capture the one actionable assigned-review observation."""

        def __init__(self, **values: object) -> None:
            self.values = values

        def append(self, _home: Path) -> None:
            observed.append(self.values)

    namespace = {
        "_POOL_V2_ADMISSIONS": admissions,
        "_claim_rows": cached_rows,
        "sh": lambda *_args: "review-session",
        "active_worker_units": lambda: set(),
        "_load_outcomes": lambda: {},
        "event_rows": repeated_event_probe,
        "reconcile_fanout_receipt": lambda _home, cid, **_kwargs: reconciled.append(cid),
        "lifecycle_state": lambda _cid: "open",
        "_current_claim_identity_fresh": lambda _cid: (None, None, None),
        "MeroObservation": Observation,
        "hashlib": __import__("hashlib"),
        "json": json,
        "re": re,
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

    assert simulated_probe_seconds == 0
    assert reconciled == [fanout_id]
    assert [item["card_id"] for item in observed] == [assigned_id]
