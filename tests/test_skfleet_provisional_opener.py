"""Deterministic coverage for the bounded provisional review opener."""

from __future__ import annotations

import ast
import datetime
import glob
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_log_once_per_hour",
    "_event_sort_key",
    "_generation_invalidated",
    "_matching_outcome_events",
    "_outcome_event_value",
    "_parent_review_generation",
    "_review_parent_ids",
    "_reviews_by_parent",
    "_review_card_id",
    "_record_review_refusal",
    "_provisional_candidate",
    "_eligible_provisional_reviews",
    "_authoritative_review_readback",
    "open_provisional_reviews",
}
CONSTANTS = {
    "_PROVISIONAL_PASS_RE",
    "_REVIEW_TITLE_RE",
    "_ID_RE",
    "_GOVERNOR_REFUSAL_RE",
    "_REVIEW_READBACK_BLOCKED",
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
        "datetime": datetime,
        "_REVIEW_REFUSALS": str(refusals),
        "HOST": "test-host",
        "HOME": str(cards.parent),
        "SKC": "skcapstone",
        "d": object(),
        "_OUTCOME_KEYS": ("verdict", "result", "disposition", "review_decision"),
        "_OUTCOME_VALUE_RE": re.compile(r"^\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)", re.I),
        "_PIPE_OUTCOME_RE": re.compile(
            r"(?:^|\|)\s*(PASS(?:_FOR_[A-Z_]+)?|FAIL|BLOCKED)\s*(?:\||$)", re.I
        ),
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
    """Small authoritative board and subprocess seam for opener tests."""

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
        self.results: list[_Result] = []
        self.suppress_create: set[int] = set()
        self.ns.update(
            {
                "_load_outcomes": lambda: self.outcomes,
                "_load_evidence_events": lambda: self.events,
                "event_rows": lambda cid: self.events.get(cid, []),
                "_native_outcome_value": lambda event: str(event.get("verdict") or ""),
                "lifecycle_state": lambda cid: self.states.get(cid, "open"),
                "folded_labels": lambda cid, core: core.get("initial_labels", []),
                "log": lambda _dest, value: self.logs.append(value),
                "subprocess": type("Subprocess", (), {"run": self._run}),
                "_rows": {},
            }
        )

    def card(self, card_id: str, title: str, *labels: str, description: str = "") -> None:
        path = self.cards / card_id
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps(
                {
                    "id": card_id,
                    "title": title,
                    "description": description,
                    "initial_labels": list(labels),
                }
            ),
            encoding="utf-8",
        )

    def outcome(
        self,
        card_id: str,
        *,
        writer: str = "pi-codex-source",
        verdict: str = "PASS_FOR_REVIEW",
        timestamp: str = "2026-09-01T12:00:00Z",
    ) -> None:
        artifact = self.cards.parent / f"{card_id}.patch"
        artifact.write_text(f"candidate {card_id}\n", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.card(card_id, f"Implementation {card_id}")
        self.outcomes[card_id] = (timestamp, verdict)
        self.events[card_id] = [
            {
                "action": "evidence",
                "ts": timestamp,
                "writer": writer,
                "verdict": verdict,
                "candidate_path": str(artifact),
                "candidate_sha256": digest,
            }
        ]

    def _run(self, command: list[str], **_kwargs: object) -> _Result:
        index = len(self.calls)
        self.calls.append(command)
        result = self.results[index] if index < len(self.results) else _Result()
        if result.returncode or index in self.suppress_create:
            return result
        review_id = command[command.index("--id") + 1]
        title = command[command.index("--title") + 1]
        description = command[command.index("--desc") + 1]
        labels = [command[i + 1] for i, value in enumerate(command) if value == "--tag"]
        self.card(review_id, title, *labels, description=description)
        return result

    def open(self, capacity: int, *, dry_run: bool = False) -> int:
        return int(self.ns["open_provisional_reviews"](capacity, dry_run=dry_run))


def test_zero_capacity_dry_run_is_empty(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")

    assert board.open(0, dry_run=True) == 0
    assert board.calls == []
    assert any("capacity=0|eligible=0|batch=0|dry_run=true" in row for row in board.logs)


def test_dry_run_bounds_batch_by_free_slots_and_eligible_sources(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    for card_id in ("a0000001", "a0000002", "a0000003", "a0000004"):
        board.outcome(card_id)

    assert board.open(2, dry_run=True) == 2
    assert board.calls == []
    assert sum("WOULD_OPEN_REVIEW" in row for row in board.logs) == 2


def test_mixed_eligibility_excludes_existing_nonterminal_review(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a0000001")
    board.outcome("a0000002")
    board.outcome("a0000003", verdict="PASS")
    board.card("b0000001", "[REREVIEW] Existing", "parent-a0000002")

    assert board.open(5, dry_run=True) == 1
    assert "a0000001" in next(row for row in board.logs if "WOULD_OPEN_REVIEW" in row)


def test_created_review_has_exact_lineage_evidence_and_distinctness(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", writer="pi-codex-source")

    assert board.open(1) == 1
    command = board.calls[0]
    labels = [command[i + 1] for i, value in enumerate(command) if value == "--tag"]
    assert [label for label in labels if label.startswith("parent-")] == ["parent-a1b2c3d4"]
    description = command[command.index("--desc") + 1]
    assert "Producer identity: pi-codex-source." in description
    assert "Candidate evidence:" in description and "sha256=" in description
    criteria = [command[i + 1] for i, value in enumerate(command) if value == "--criteria"]
    assert "Reviewer identity must differ from source implementer pi-codex-source." in criteria

    assert board.open(1) == 0
    assert len(board.calls) == 1


def test_missing_or_hash_mismatched_candidate_fails_closed(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    board.events["a1b2c3d4"][0]["candidate_sha256"] = "0" * 64

    assert board.open(1) == 0
    assert board.calls == []
    assert any("OPEN_REVIEW_EVIDENCE_BLOCKED" in row for row in board.logs)


def test_native_verdict_accepts_hash_verified_embedded_candidate(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    timestamp = "2026-09-02T20:02:29+00:00"
    artifact = tmp_path / "candidate.tar"
    artifact.write_bytes(b"candidate bytes")
    board.card("a1b2c3d4", "Implementation a1b2c3d4")
    board.outcomes["a1b2c3d4"] = (timestamp, "PASS_FOR_REVIEW")
    board.events["a1b2c3d4"] = [
        {
            "action": "verdict",
            "ts": timestamp,
            "writer": "pi-mero-source",
            "verdict": "PASS_FOR_REVIEW",
            "evidence_links": [
                {
                    "type": "candidate_tree",
                    "path": str(artifact),
                    "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                }
            ],
        }
    ]

    assert board.open(1) == 1
    description = board.calls[0][board.calls[0].index("--desc") + 1]
    assert "Producer identity: pi-mero-source." in description
    assert "Candidate evidence: %s sha256=" % artifact in description


def test_native_verdict_resolves_hash_verified_candidate_manifest(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    timestamp = "2026-09-02T17:22:19+00:00"
    artifact = tmp_path / "candidate.tar"
    artifact.write_bytes(b"candidate bytes")
    manifest = tmp_path / "verdict-evidence.json"
    manifest.write_text(
        json.dumps(
            {
                "evidence_links": [
                    {
                        "type": "candidate_tree",
                        "path": str(artifact),
                        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    board.card("a1b2c3d4", "Implementation a1b2c3d4")
    board.outcomes["a1b2c3d4"] = (timestamp, "PASS_FOR_REVIEW")
    board.events["a1b2c3d4"] = [
        {
            "action": "verdict",
            "ts": timestamp,
            "writer": "pi-mero-source",
            "verdict": "PASS_FOR_REVIEW",
            "evidence_path": str(manifest),
            "artifact_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        }
    ]

    assert board.open(1) == 1
    description = board.calls[0][board.calls[0].index("--desc") + 1]
    assert "Candidate evidence: %s sha256=" % artifact in description


def test_unverified_embedded_or_manifest_candidate_fails_closed(tmp_path: Path) -> None:
    for mode in ("embedded", "manifest"):
        board = OpenerHarness(tmp_path / mode)
        board.outcome("a1b2c3d4")
        event = board.events["a1b2c3d4"][0]
        event.pop("candidate_path")
        event.pop("candidate_sha256")
        bad_link = {
            "type": "candidate_tree",
            "path": str(tmp_path / mode / "missing.tar"),
            "sha256": "0" * 64,
        }
        if mode == "embedded":
            event["evidence_links"] = [bad_link]
        else:
            manifest = tmp_path / mode / "manifest.json"
            manifest.write_text(json.dumps({"evidence_links": [bad_link]}), encoding="utf-8")
            event["evidence_path"] = str(manifest)
            event["artifact_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()

        assert board.open(1) == 0
        assert board.calls == []
        assert any("OPEN_REVIEW_EVIDENCE_BLOCKED" in row for row in board.logs)


def test_separate_candidate_evidence_joins_exact_outcome_and_producer(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", writer="pi-codex-source")
    event = board.events["a1b2c3d4"][0]
    artifact = event.pop("candidate_path")
    digest = event.pop("candidate_sha256")
    board.events["a1b2c3d4"].append(
        {
            "action": "review_candidate_evidence",
            "ts": "2026-09-03T12:00:00Z",
            "writer": "repair-agent",
            "source_outcome_ts": event["ts"],
            "source_verdict": event["verdict"],
            "producer": event["writer"],
            "candidate_path": artifact,
            "candidate_sha256": digest,
        }
    )

    assert board.open(1) == 1
    description = board.calls[0][board.calls[0].index("--desc") + 1]
    assert "Producer identity: pi-codex-source." in description
    assert "Candidate evidence: %s sha256=%s." % (artifact, digest) in description


def test_separate_candidate_evidence_wrong_join_or_duplicate_fails_closed(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    event = board.events["a1b2c3d4"][0]
    artifact = event.pop("candidate_path")
    digest = event.pop("candidate_sha256")
    supplemental = {
        "action": "review_candidate_evidence",
        "ts": "2026-09-03T12:00:00Z",
        "writer": "repair-agent",
        "source_outcome_ts": event["ts"],
        "source_verdict": event["verdict"],
        "producer": "wrong-producer",
        "candidate_path": artifact,
        "candidate_sha256": digest,
    }
    board.events["a1b2c3d4"].append(supplemental)

    assert board.open(1) == 0
    supplemental["producer"] = event["writer"]
    board.events["a1b2c3d4"].append(dict(supplemental, writer="another-repair-agent"))
    assert board.open(1) == 0


def test_typed_candidate_identity_is_carried_into_review(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    board.events["a1b2c3d4"][0].update(
        {
            "candidate_commit": "1" * 40,
            "candidate_tree": "2" * 40,
            "candidate_ref": "refs/heads/review/a1b2c3d4",
        }
    )

    assert board.open(1) == 1
    description = board.calls[0][board.calls[0].index("--desc") + 1]
    assert "Candidate commit: %s." % ("1" * 40) in description
    assert "Candidate tree: %s." % ("2" * 40) in description
    assert "Candidate ref: refs/heads/review/a1b2c3d4." in description


def test_partial_typed_candidate_identity_fails_closed(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    board.events["a1b2c3d4"][0]["candidate_commit"] = "1" * 40

    assert board.open(1) == 0
    assert board.calls == []
    assert any("OPEN_REVIEW_EVIDENCE_BLOCKED" in row for row in board.logs)


def test_partial_create_failure_stops_without_spending_extra_budget(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    for card_id in ("a0000001", "a0000002", "a0000003"):
        board.outcome(card_id)
    board.results = [_Result(), _Result(returncode=1, stderr="transport failed")]

    assert board.open(3) == 1
    assert len(board.calls) == 2
    assert any("OPEN_REVIEW_FAILED" in row for row in board.logs)


def test_stale_readback_blocks_launch_eligibility_and_stops(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a0000001")
    board.outcome("a0000002")
    board.suppress_create.add(0)

    assert board.open(2) == 0
    assert len(board.calls) == 1
    review_id = board.calls[0][board.calls[0].index("--id") + 1]
    assert review_id in board.ns["_REVIEW_READBACK_BLOCKED"]
    assert any("OPEN_REVIEW_STALE_READBACK" in row for row in board.logs)


def test_capacity_bound_counts_attempts_not_only_successes(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    for card_id in ("a0000001", "a0000002", "a0000003"):
        board.outcome(card_id)
    board.results = [_Result(returncode=1, stderr="governed card requires exactly one parent-")]

    assert board.open(1) == 0
    assert len(board.calls) == 1


def test_deterministic_parent_generation_id(tmp_path: Path) -> None:
    first = OpenerHarness(tmp_path / "first")
    second = OpenerHarness(tmp_path / "second")
    for board in (first, second):
        board.outcome("a1b2c3d4")
        assert board.open(1, dry_run=True) == 1

    first_id = first.logs[-1].rsplit("review=", 1)[1]
    second_id = second.logs[-1].rsplit("review=", 1)[1]
    assert first_id == second_id
