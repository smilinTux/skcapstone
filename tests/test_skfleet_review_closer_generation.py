"""Generation fencing for automatic review closeout."""

import ast
import hashlib
import json
import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
FUNCTIONS = {
    "_event_sort_key",
    "_event_identity",
    "_generation_invalidated",
    "_matching_outcome_events",
    "_outcome_event_value",
    "_parent_review_generation",
    "_review_names_generation",
    "_review_join_value",
    "_has_review_join",
    "close_reviewed_parents",
}


class Result:
    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr


class Harness:
    def __init__(self, root: Path) -> None:
        self.cards = root / "cards"
        self.cards.mkdir()
        self.events: dict[str, list[dict[str, str]]] = {}
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.states: dict[str, str] = {}
        self.reviews: dict[str, set[str]] = {}
        self.calls: list[list[str]] = []
        self.fail_complete_once = False
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        nodes = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
        ]
        self.ns = {
            "CARDS": str(self.cards),
            "HOST": "chiap08",
            "SKC": "skcapstone",
            "d": object(),
            "hashlib": hashlib,
            "json": json,
            "os": __import__("os"),
            "_OUTCOME_KEYS": ("verdict", "result", "disposition", "review_decision"),
            "_OUTCOME_VALUE_RE": re.compile(r"^\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)", re.I),
            "_PIPE_OUTCOME_RE": re.compile(
                r"(?:^|\|)\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)\s*(?:\||$)", re.I
            ),
            "_PROVISIONAL_PASS_RE": re.compile(
                r"^\s*(PASS_FOR_[A-Z_]+|PASS_READY_[A-Z_]+)\b", re.I
            ),
            "_PASS_ONLY_RE": re.compile(r"^\s*PASS(?!_FOR)", re.I),
            "_fold_key": lambda value: str(value or "").lower(),
            "_native_outcome_value": lambda event: event.get("verdict"),
        }
        exec(compile(ast.Module(nodes, type_ignores=[]), str(SCRIPT), "exec"), self.ns)
        self.ns.update(
            {
                "event_rows": lambda card_id: self.events.get(card_id, []),
                "_load_outcomes": lambda: self.outcomes,
                "_provisional_candidate": lambda *_args: (
                    "producer",
                    "/candidate",
                    "a" * 64,
                    "b" * 40,
                    "c" * 40,
                    "refs/heads/candidate",
                ),
                "_reviews_by_parent": lambda: self.reviews,
                "lifecycle_state": lambda card_id: self.states.get(card_id, "open"),
                "subprocess": type("Subprocess", (), {"run": self.run}),
                "log": lambda *_args: None,
                "_rows": {},
            }
        )

    def run(self, command: list[str], **_kwargs: object) -> Result:
        self.calls.append(command)
        if "link" in command and "review_join" in command:
            card_id = command[command.index("link") + 1]
            self.events.setdefault(card_id, []).append(
                {
                    "action": "link",
                    "link_key": "review_join",
                    "link_value": command[command.index("review_join") + 1],
                    "writer": "fleet-review-closer",
                    "ts": "2026-09-03T18:46:00Z",
                }
            )
        if "complete" in command:
            if self.fail_complete_once:
                self.fail_complete_once = False
                return Result(1, "transient completion failure")
            self.states[command[command.index("complete") + 1]] = "complete"
        return Result()

    def source(self, card_id: str = "efa30b41") -> str:
        timestamp = "2026-09-03T18:40:00Z"
        (self.cards / card_id).mkdir()
        event = {
            "action": "verdict",
            "card_id": card_id,
            "ts": timestamp,
            "writer": "producer",
            "verdict": "PASS_FOR_REVIEW",
        }
        self.events[card_id] = [event]
        self.outcomes[card_id] = (timestamp, "PASS_FOR_REVIEW")
        self.states[card_id] = "open"
        generation = self.ns["_parent_review_generation"](card_id, timestamp, "PASS_FOR_REVIEW")
        assert generation
        return generation[0]

    def review(self, generation: str, parent: str = "efa30b41", review: str = "1234abcd") -> None:
        timestamp = "2026-09-03T18:45:00Z"
        directory = self.cards / review
        directory.mkdir()
        (directory / "core.json").write_text(
            json.dumps({"description": f"Outcome generation: {generation}."}),
            encoding="utf-8",
        )
        self.events[review] = [
            {
                "action": "verdict",
                "card_id": review,
                "ts": timestamp,
                "writer": "reviewer",
                "verdict": "PASS",
            }
        ]
        self.outcomes[review] = (timestamp, "PASS")
        self.states[review] = "complete"
        self.reviews[parent] = {review}


def test_amended_and_reopened_parent_cannot_reuse_historical_pass(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    generation = harness.source()
    harness.review(generation)
    harness.events["efa30b41"].extend(
        [
            {
                "action": "describe",
                "ts": "2026-09-03T18:49:00Z",
                "writer": "jarvis",
            },
            {
                "action": "move",
                "column": "backlog",
                "ts": "2026-09-03T18:49:01Z",
                "writer": "jarvis",
            },
        ]
    )

    assert harness.ns["close_reviewed_parents"]() == 0
    assert harness.states["efa30b41"] == "open"


def test_every_later_source_mutation_invalidates_the_review_join(tmp_path: Path) -> None:
    mutations = [
        {"action": "amend_criteria"},
        {"action": "add_dependency"},
        {"action": "remove_dependency"},
        {"action": "evidence"},
        {"action": "review_candidate_evidence", "source_outcome_ts": "new"},
        {"action": "verdict", "verdict": "PASS_FOR_REREVIEW"},
        {"action": "move", "column": "doing"},
    ]
    for index, mutation in enumerate(mutations):
        root = tmp_path / str(index)
        root.mkdir()
        harness = Harness(root)
        generation = harness.source()
        harness.review(generation)
        harness.events["efa30b41"].append(
            {
                **mutation,
                "ts": "2026-09-03T18:49:00Z",
                "writer": "producer",
            }
        )

        assert harness.ns["close_reviewed_parents"]() == 0


def test_unchanged_exact_generation_closes_once(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    generation = harness.source("aaaabbbb")
    harness.review(generation, parent="aaaabbbb")

    assert harness.ns["close_reviewed_parents"]() == 1
    assert harness.ns["close_reviewed_parents"]() == 0
    assert harness.states["aaaabbbb"] == "complete"
    assert sum("complete" in call for call in harness.calls) == 1


def test_non_authority_host_never_closes(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    generation = harness.source()
    harness.review(generation)
    harness.ns["HOST"] = "chiap01"

    assert harness.ns["close_reviewed_parents"]() == 0
    assert harness.calls == []


def test_exact_join_survives_transient_completion_failure(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    generation = harness.source()
    harness.review(generation)
    harness.fail_complete_once = True

    assert harness.ns["close_reviewed_parents"]() == 0
    assert harness.ns["close_reviewed_parents"]() == 1
    assert sum("review_join" in call for call in harness.calls) == 1
    assert sum("complete" in call for call in harness.calls) == 2


def test_review_pass_is_invalid_after_review_amendment(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    generation = harness.source()
    harness.review(generation)
    harness.events["1234abcd"].append(
        {
            "action": "amend_criteria",
            "ts": "2026-09-03T18:46:00Z",
            "writer": "reviewer",
        }
    )

    assert harness.ns["close_reviewed_parents"]() == 0


def test_mismatched_review_generation_fails_closed(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.source()
    harness.review("0" * 64)

    assert harness.ns["close_reviewed_parents"]() == 0
