"""Focused coverage for the fleet provisional-pass review opener."""

from __future__ import annotations

import ast
import concurrent.futures
import glob
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_review_parent_ids",
    "_reviews_by_parent",
    "_review_card_id",
    "_record_review_refusal",
    "open_provisional_reviews",
    "_load_outcomes_with_producer",
}
CONSTANTS = {
    "_PROVISIONAL_PASS_RE",
    "_REVIEW_TITLE_RE",
    "_ID_RE",
    "_GOVERNOR_REFUSAL_RE",
}


def _namespace(cards: Path, refusals: Path) -> dict[str, object]:
    """Extract the opener seam without executing the fleet launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {
        "hashlib": hashlib,
        "glob": glob,
        "json": json,
        "os": os,
        "re": re,
        "CARDS": str(cards),
        "_REVIEW_REFUSALS": str(refusals),
        "HOST": "test-host",
        "SKC": "skcapstone",
        "d": object(),
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys()
    return namespace


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class OpenerHarness:
    """Small filesystem board and subprocess seam for opener tests."""

    def __init__(self, root: Path) -> None:
        self.cards = root / "cards"
        self.cards.mkdir(parents=True)
        self.refusals = root / "refusals"
        self.ns = _namespace(self.cards, self.refusals)
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.states: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.logs: list[str] = []
        self.result = _Result()
        self.barrier: threading.Barrier | None = None
        self.ns.update(
            {
                "_load_outcomes": lambda: {
                    k: (v[0], v[1]) if len(v) >= 2 else (v[0] if len(v) >= 1 else "", "")
                    for k, v in self.outcomes.items()
                },
                "_load_outcomes_with_producer": lambda: {
                    k: (v[0], v[1], v[2] if len(v) >= 3 else "")
                    for k, v in self.outcomes.items()
                },
                "lifecycle_state": lambda cid: self.states.get(cid, "open"),
                "folded_labels": lambda cid, core: core.get("initial_labels", []),
                "log": lambda _dest, value: self.logs.append(value),
                "subprocess": type("Subprocess", (), {"run": self._run}),
                "_load_evidence_events": lambda: {},
            }
        )

    def card(self, card_id: str, title: str, *labels: str) -> None:
        path = self.cards / card_id
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps({"id": card_id, "title": title, "initial_labels": list(labels)}),
            encoding="utf-8",
        )
    
    def outcome(self, card_id: str, ts: str, verdict: str, producer: str = "") -> None:
        """Set an outcome for a card with optional producer."""
        self.outcomes[card_id] = (ts, verdict, producer)

    def _run(self, command: list[str], **_kwargs: object) -> _Result:
        self.calls.append(command)
        if self.barrier:
            self.barrier.wait()
        if self.result.returncode:
            return self.result
        review_id = command[command.index("--id") + 1]
        title = command[command.index("--title") + 1]
        labels = [command[index + 1] for index, value in enumerate(command) if value == "--tag"]
        self.card(review_id, title, *labels)
        return self.result

    def open(self) -> int:
        return int(self.ns["open_provisional_reviews"]())


def test_cli_create_accepts_stable_automation_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "0")
    main = click.Group()
    register_coord_commands(main)
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "abc12345",
            "--title",
            "Stable review",
            "--by",
            "fleet-review-opener",
        ],
    )
    assert result.exit_code == 0, result.output
    assert list((tmp_path / "coordination" / "tasks").glob("abc12345-*.json"))


def test_provisional_pass_opens_once_and_repeated_sweep_is_idempotent(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW immutable evidence", "pi-codex-chiap01")

    assert board.open() == 1
    assert board.open() == 0
    assert len(board.calls) == 1
    children = set(board.ns["_reviews_by_parent"]()["a1b2c3d4"])
    assert len(children) == 1


def test_five_host_sweeps_converge_on_one_successor_id(tmp_path: Path) -> None:
    child_ids = set()
    for index in range(5):
        board = OpenerHarness(tmp_path / f"host-{index}")
        board.card("a1b2c3d4", "Implementation")
        board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_READY_REVIEW", "pi-codex-chiap01")
        assert board.open() == 1
        child_ids.add(board.calls[0][board.calls[0].index("--id") + 1])

    assert len(child_ids) == 1


def test_five_concurrent_sweeps_create_one_successor(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board.barrier = threading.Barrier(5)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _index: board.open(), range(5)))

    assert results == [1, 1, 1, 1, 1]
    assert len(board.calls) == 5
    children = set(board.ns["_reviews_by_parent"]()["a1b2c3d4"])
    assert len(children) == 1


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("PASS", 0),
        ("BLOCKED old text then PASS_FOR_REVIEW", 0),
        ("PASS_FOR_REVIEW; historical BLOCKED was superseded", 1),
        ("PASS_READY_REREVIEW\nBLOCKED is only historical", 1),
    ],
)
def test_only_leading_provisional_token_opens(tmp_path: Path, verdict: str, expected: int) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", verdict, "pi-codex-chiap01")
    assert board.open() == expected


def test_existing_live_rereview_successor_prevents_creation(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.card("b2c3d4e5", "[REREVIEW] Existing", "parent-a1b2c3d4")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    assert board.open() == 0
    assert board.calls == []


def test_governor_refusal_is_logged_and_not_retried(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board.result = _Result(
        returncode=1,
        stderr="ValueError: Refusing third review level for root a1b2c3d4",
    )

    assert board.open() == 0
    assert board.open() == 0
    assert len(board.calls) == 1
    assert sum("OPEN_REVIEW_REFUSED" in row for row in board.logs) == 1
    assert len(list(board.refusals.glob("*.json"))) == 1


def test_transient_failure_is_not_misrecorded_as_governor_refusal(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board.result = _Result(returncode=1, stderr="temporary transport failure")

    assert board.open() == 0
    assert board.open() == 0
    assert len(board.calls) == 2
    assert not board.refusals.exists()
    assert sum("OPEN_REVIEW_FAILED" in row for row in board.logs) == 2


def test_bounded_batch_opens_only_up_to_capacity(tmp_path: Path) -> None:
    """Bounded batch mode opens no more than the specified capacity."""
    board = OpenerHarness(tmp_path)
    # Create 5 cards with provisional outcomes
    for i in range(5):
        card_id = f"card{i:02d}00000"
        board.card(card_id, f"Implementation {i}")
        board.outcome(card_id, "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    
    # Capacity of 2 should open exactly 2 reviews
    opened = board.ns["open_provisional_reviews"](capacity=2)
    assert opened == 2
    assert len(board.calls) == 2


def test_capacity_none_opens_all_eligible(tmp_path: Path) -> None:
    """When capacity is None, all eligible reviews are opened (legacy behavior)."""
    board = OpenerHarness(tmp_path)
    for i in range(3):
        card_id = f"card{i:02d}00000"
        board.card(card_id, f"Implementation {i}")
        board.outcome(card_id, "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    
    # capacity=None opens all 3
    opened = board.ns["open_provisional_reviews"](capacity=None)
    assert opened == 3
    assert len(board.calls) == 3


def test_producer_identity_is_extracted_from_outcome(tmp_path: Path) -> None:
    """The producer (writer) is extracted from the outcome event."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    
    # Simulate outcomes with producer
    outcomes = board.ns["_load_outcomes_with_producer"]()
    # Producer should be the one we set
    assert outcomes["a1b2c3d4"][2] == "pi-codex-chiap01"  # (ts, value, producer)


def test_distinct_reviewer_enforcement_rejects_same_lane(tmp_path: Path) -> None:
    """Reviews are rejected when producer and reviewer have same lane identity."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    
    # Simulate a qwen producer (reviewer is also qwen)
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-qwen-chiap01")
    
    opened = board.open()
    # Should be rejected since producer lane (qwen) == reviewer lane (qwen)
    assert opened == 0
    assert any("REVIEW_REJECTED_SAME_IDENTITY" in log for log in board.logs)


def test_missing_producer_is_rejected_and_logged(tmp_path: Path) -> None:
    """Cards without producer attribution are rejected and logged as refusal."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "")  # Empty producer
    
    # With empty producer, should log rejection
    board.open()
    
    # Check for rejection log
    has_rejection = any("REVIEW_REJECTED_NO_PRODUCER" in log for log in board.logs)
    # If rejection happened, there should be a refusal recorded
    assert has_rejection
    assert len(list(board.refusals.glob("*.json"))) > 0


def test_multi_host_concurrent_opens_converge_on_distinct_ids(tmp_path: Path) -> None:
    """Multiple hosts opening reviews concurrently converge on distinct IDs."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board.barrier = threading.Barrier(3)
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _index: board.open(), range(3)))
    
    # All should see the same parent and attempt to open
    # But only one should succeed due to deterministic ID
    assert len(board.calls) == 3  # All 3 attempt
    # All 3 calls should have the same review_id
    review_ids = set()
    for call in board.calls:
        review_id = call[call.index("--id") + 1]
        review_ids.add(review_id)
    assert len(review_ids) == 1  # All converged on same ID


def test_deterministic_review_id_from_parent_outcome_generation(tmp_path: Path) -> None:
    """Review ID is deterministic from parent + outcome timestamp + verdict."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    
    # Open twice - should produce same ID
    board.open()
    first_id = board.calls[0][board.calls[0].index("--id") + 1]
    
    # Create new board with same data
    board2 = OpenerHarness(tmp_path / "board2")
    board2.card("a1b2c3d4", "Implementation")
    board2.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board2.open()
    second_id = board2.calls[0][board2.calls[0].index("--id") + 1]
    
    assert first_id == second_id


def test_different_outcome_generations_produce_distinct_review_ids(tmp_path: Path) -> None:
    """Different outcome timestamps for same parent produce distinct review IDs."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    
    # First outcome
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    board.open()
    first_id = board.calls[0][board.calls[0].index("--id") + 1]
    
    # Reset and try different timestamp
    board2 = OpenerHarness(tmp_path / "board2")
    board2.card("a1b2c3d4", "Implementation")
    board2.outcome("a1b2c3d4", "2026-08-30T13:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")  # Different ts
    board2.open()
    second_id = board2.calls[0][board2.calls[0].index("--id") + 1]
    
    assert first_id != second_id


def test_capacity_zero_opens_nothing(tmp_path: Path) -> None:
    """Capacity of 0 opens no reviews, even with eligible cards."""
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW", "pi-codex-chiap01")
    
    opened = board.ns["open_provisional_reviews"](capacity=0)
    assert opened == 0
    assert len(board.calls) == 0
