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
    "_provisional_producer",
    "open_provisional_reviews",
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
        self.events: dict[str, list[dict[str, str]]] = {}
        self.calls: list[list[str]] = []
        self.logs: list[str] = []
        self.result = _Result()
        self.barrier: threading.Barrier | None = None
        self.ns.update(
            {
                "_load_outcomes": lambda: self.outcomes,
                "_load_evidence_events": lambda: self.events,
                "event_rows": lambda cid: self.events.get(cid, []),
                "_native_outcome_value": lambda event: event.get("verdict", ""),
                "_fold_key": lambda key: str(key or "").lower(),
                "_OUTCOME_KEYS": ("verdict", "result", "disposition", "review_decision"),
                "_OUTCOME_VALUE_RE": re.compile(
                    r"^\\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)", re.I
                ),
                "_PIPE_OUTCOME_RE": re.compile(
                    r"(?:^|\\|)\\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)\\s*(?:\\||$)",
                    re.I,
                ),
                "lifecycle_state": lambda cid: self.states.get(cid, "open"),
                "folded_labels": lambda cid, core: core.get("initial_labels", []),
                "log": lambda _dest, value: self.logs.append(value),
                "subprocess": type("Subprocess", (), {"run": self._run}),
            }
        )

    def card(self, card_id: str, title: str, *labels: str) -> None:
        path = self.cards / card_id
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps({"id": card_id, "title": title, "initial_labels": list(labels)}),
            encoding="utf-8",
        )

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

    def outcome(
        self,
        card_id: str,
        verdict: str,
        *,
        timestamp: str = "2026-08-30T12:00:00Z",
        writer: str | None = "producer-agent",
    ) -> None:
        self.outcomes[card_id] = (timestamp, verdict)
        event = {"action": "verdict", "ts": timestamp, "verdict": verdict}
        if writer is not None:
            event["writer"] = writer
        self.events.setdefault(card_id, []).append(event)

    def open(self, capacity: int = 10) -> int:
        return int(self.ns["open_provisional_reviews"](capacity))


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
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW immutable evidence")

    assert board.open() == 1
    assert board.open() == 0
    assert len(board.calls) == 1
    children = set(board.ns["_reviews_by_parent"]()["a1b2c3d4"])
    assert len(children) == 1


def test_batch_is_bounded_by_free_review_lane_capacity(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    for index in range(6):
        card_id = f"a1b2c3{index:02x}"
        board.card(card_id, f"Implementation {index}")
        board.outcome(card_id, "PASS_FOR_REVIEW")

    assert board.open(capacity=3) == 3
    assert len(board.calls) == 3
    assert {
        call[call.index("--title") + 1].rsplit(" ", 1)[-1]
        for call in board.calls
    } == {"a1b2c300", "a1b2c301", "a1b2c302"}


def test_zero_or_invalid_capacity_fails_closed(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW")

    assert board.open(capacity=0) == 0
    assert board.ns["open_provisional_reviews"]("invalid") == 0
    assert board.calls == []


def test_five_host_sweeps_converge_on_one_successor_id(tmp_path: Path) -> None:
    child_ids = set()
    for index in range(5):
        board = OpenerHarness(tmp_path / f"host-{index}")
        board.card("a1b2c3d4", "Implementation")
        board.outcome("a1b2c3d4", "PASS_READY_REVIEW")
        assert board.open() == 1
        child_ids.add(board.calls[0][board.calls[0].index("--id") + 1])

    assert len(child_ids) == 1


def test_five_concurrent_sweeps_create_one_successor(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW")
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
    board.outcome("a1b2c3d4", verdict)
    assert board.open() == expected


def test_generation_change_produces_a_distinct_deterministic_id(tmp_path: Path) -> None:
    first = OpenerHarness(tmp_path / "first")
    first.card("a1b2c3d4", "Implementation")
    first.outcome("a1b2c3d4", "PASS_FOR_REVIEW", timestamp="2026-08-30T12:00:00Z")
    assert first.open() == 1

    second = OpenerHarness(tmp_path / "second")
    second.card("a1b2c3d4", "Implementation")
    second.outcome("a1b2c3d4", "PASS_FOR_REVIEW", timestamp="2026-08-30T13:00:00Z")
    assert second.open() == 1

    first_id = first.calls[0][first.calls[0].index("--id") + 1]
    second_id = second.calls[0][second.calls[0].index("--id") + 1]
    assert first_id != second_id


def test_distinct_reviewer_contract_names_exact_producer(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW", writer="producer-007")

    assert board.open() == 1
    command = board.calls[0]
    criteria = [command[index + 1] for index, value in enumerate(command) if value == "--criteria"]
    assert "Reviewer identity must differ from producer producer-007." in criteria
    assert "Producer identity: producer-007." in command[command.index("--desc") + 1]


@pytest.mark.parametrize("writers", [[], [None], ["producer-a", "producer-b"]])
def test_missing_or_ambiguous_producer_attribution_fails_closed(
    tmp_path: Path, writers: list[str | None]
) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcomes["a1b2c3d4"] = ("2026-08-30T12:00:00Z", "PASS_FOR_REVIEW")
    for writer in writers:
        event = {
            "action": "verdict",
            "ts": "2026-08-30T12:00:00Z",
            "verdict": "PASS_FOR_REVIEW",
        }
        if writer is not None:
            event["writer"] = writer
        board.events.setdefault("a1b2c3d4", []).append(event)

    assert board.open() == 0
    assert board.calls == []
    assert any("OPEN_REVIEW_ATTRIBUTION_BLOCKED" in row for row in board.logs)


def test_existing_live_rereview_successor_prevents_creation(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.card("b2c3d4e5", "[REREVIEW] Existing", "parent-a1b2c3d4")
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW")
    assert board.open() == 0
    assert board.calls == []


def test_governor_refusal_is_logged_and_not_retried(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.card("a1b2c3d4", "Implementation")
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW")
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
    board.outcome("a1b2c3d4", "PASS_FOR_REVIEW")
    board.result = _Result(returncode=1, stderr="temporary transport failure")

    assert board.open() == 0
    assert board.open() == 0
    assert len(board.calls) == 2
    assert not board.refusals.exists()
    assert sum("OPEN_REVIEW_FAILED" in row for row in board.logs) == 2
