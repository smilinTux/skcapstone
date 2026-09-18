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

from skcapstone.review_admission import (
    governed_review_gate_reasons,
    governed_review_seat,
    qualified_reviewer_seats,
)

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_log_once_per_hour",
    "_event_sort_key",
    "_event_identity",
    "_generation_invalidated",
    "_matching_outcome_events",
    "_outcome_event_value",
    "_parent_review_generation",
    "_review_parent_ids",
    "_reviews_by_parent",
    "_review_card_id",
    "_record_review_refusal",
    "_provisional_candidate",
    "_outcome_scan_rows",
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
        "governed_review_gate_reasons": governed_review_gate_reasons,
        "governed_review_seat": governed_review_seat,
        "qualified_reviewer_seats": qualified_reviewer_seats,
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
        # Production keeps card events in two stores: the structure store
        # (``cards/<id>/events``, read by ``event_rows``) and the legacy kanban
        # overlay (``coordination/card_events/*.jsonl``, read by
        # ``_load_evidence_events``).  Keeping one dict for both would hide
        # every defect that only shows up when an outcome lives in exactly one
        # of them, so the harness models them separately and merely defaults
        # the structure store to the overlay content.
        self.events: dict[str, list[dict[str, str]]] = {}
        self.structure: dict[str, list[dict[str, str]]] = {}
        self.calls: list[list[str]] = []
        self.logs: list[str] = []
        self.results: list[_Result] = []
        self.suppress_create: set[int] = set()
        self.suppress_meta: set[int] = set()
        self.ns.update(
            {
                "_load_outcomes": lambda: self.outcomes,
                "_load_evidence_events": lambda: self.events,
                "event_rows": lambda cid: self.structure.get(cid, self.events.get(cid, [])),
                "_native_outcome_value": lambda event: str(event.get("verdict") or ""),
                "lifecycle_state": lambda cid: self.states.get(cid, "open"),
                "folded_labels": lambda cid, core: core.get("initial_labels", []),
                "log": lambda _dest, value: self.logs.append(value),
                "subprocess": type("Subprocess", (), {"run": self._run}),
                "_rows": {},
            }
        )

    def card(
        self,
        card_id: str,
        title: str,
        *labels: str,
        description: str = "",
        meta: dict[str, str] | None = None,
    ) -> None:
        path = self.cards / card_id
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps(
                {
                    "id": card_id,
                    "title": title,
                    "description": description,
                    "initial_labels": list(labels),
                    "meta": dict(meta or {}),
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
        typed_identity: bool = True,
    ) -> None:
        artifact = self.cards.parent / f"{card_id}.patch"
        artifact.write_text(f"candidate {card_id}\n", encoding="utf-8")
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        self.card(card_id, f"Implementation {card_id}")
        self.outcomes[card_id] = (timestamp, verdict)
        event = {
            "action": "evidence",
            "ts": timestamp,
            "writer": writer,
            "verdict": verdict,
            "candidate_path": str(artifact),
            "candidate_sha256": digest,
        }
        if typed_identity:
            event.update(
                {
                    "candidate_commit": hashlib.sha1(card_id.encode()).hexdigest(),
                    "candidate_tree": hashlib.sha1(f"tree-{card_id}".encode()).hexdigest(),
                    "candidate_ref": f"refs/heads/review/{card_id}",
                }
            )
        self.events[card_id] = [event]

    @staticmethod
    def _flag(command: list[str], name: str) -> str | None:
        return command[command.index(name) + 1] if name in command else None

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
        producer = self._flag(command, "--producer-identity")
        evidence = self._flag(command, "--candidate-evidence-sha256")
        source_card = self._flag(command, "--source-card")
        head_revision = self._flag(command, "--head-revision")
        # Mirror the `coord create` governed-review fail-fast from PR 567: a
        # review-labelled create without the seat plus complete typed metadata
        # is refused at the CLI, never silently created.
        governed = "review" in labels or "[REVIEW]" in title.upper()
        if governed:
            missing = []
            if "seat-seraph" not in labels:
                missing.append("seat-seraph")
            if not str(producer or "").strip():
                missing.append("producer_identity")
            if not re.fullmatch(r"[0-9a-fA-F]{64}", str(evidence or "")):
                missing.append("candidate_evidence_sha256")
            if not str(source_card or "").strip():
                missing.append("source_card")
            if not re.fullmatch(r"[0-9a-fA-F]{40}", str(head_revision or "")):
                missing.append("head_revision")
            if missing:
                return _Result(
                    returncode=1,
                    stderr="incomplete governed review card; missing: " + ", ".join(missing),
                )
        meta = (
            {
                "producer_identity": str(producer).strip(),
                "candidate_evidence_sha256": str(evidence).lower(),
                "link_source_card": str(source_card).strip(),
                "link_head_revision": str(head_revision).lower(),
            }
            if governed
            else {}
        )
        if index in self.suppress_meta:
            meta = {}
        self.card(review_id, title, *labels, description=description, meta=meta)
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
            "candidate_commit": "3" * 40,
            "candidate_tree": "4" * 40,
            "candidate_ref": "refs/heads/review/a1b2c3d4",
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
            "candidate_commit": "3" * 40,
            "candidate_tree": "4" * 40,
            "candidate_ref": "refs/heads/review/a1b2c3d4",
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
    board.events["a1b2c3d4"][0].pop("candidate_tree")

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


def test_created_review_declares_exactly_one_qualified_seat(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")

    assert board.open(1) == 1
    command = board.calls[0]
    labels = [command[i + 1] for i, value in enumerate(command) if value == "--tag"]
    assert [label for label in labels if label.startswith("seat-")] == ["seat-seraph"]
    review_id = command[command.index("--id") + 1]
    core = json.loads((board.cards / review_id / "core.json").read_text(encoding="utf-8"))
    seat = governed_review_seat(core["initial_labels"], qualified_reviewer_seats(core))
    assert seat == "seraph"


def test_created_review_carries_typed_source_binding(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", writer="pi-codex-source")
    commit = board.events["a1b2c3d4"][0]["candidate_commit"]
    digest = board.events["a1b2c3d4"][0]["candidate_sha256"]

    assert board.open(1) == 1
    command = board.calls[0]
    review_id = command[command.index("--id") + 1]
    core = json.loads((board.cards / review_id / "core.json").read_text(encoding="utf-8"))
    meta = core["meta"]
    assert meta["link_source_card"] == "a1b2c3d4"
    assert re.fullmatch(r"[0-9a-f]{40}", meta["link_head_revision"])
    assert meta["link_head_revision"] == commit
    assert meta["producer_identity"] == "pi-codex-source"
    assert meta["candidate_evidence_sha256"] == digest
    gate = governed_review_gate_reasons(
        {
            "title": core["title"],
            "description": core["description"],
            "links": {},
            "meta": meta,
        },
        core["initial_labels"],
    )
    assert not ({"wrong-seat", "absent-typed-metadata", "absent-source-binding"} & set(gate))


def test_source_binding_less_card_is_refused_by_gate_and_readback(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4")
    board.suppress_meta.add(0)

    assert board.open(1) == 0
    review_id = board.calls[0][board.calls[0].index("--id") + 1]
    assert review_id in board.ns["_REVIEW_READBACK_BLOCKED"]
    assert any("OPEN_REVIEW_STALE_READBACK" in row for row in board.logs)
    core = json.loads((board.cards / review_id / "core.json").read_text(encoding="utf-8"))
    gate = governed_review_gate_reasons(
        {
            "title": core["title"],
            "description": core["description"],
            "links": {},
            "meta": core["meta"],
        },
        core["initial_labels"],
    )
    assert "absent-source-binding" in gate


def test_untyped_candidate_is_skipped_with_reason_not_created(tmp_path: Path) -> None:
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", typed_identity=False)

    assert board.open(1) == 0
    assert board.calls == []
    assert any("OPEN_REVIEW_SOURCE_UNBOUND" in row for row in board.logs)


def test_outcome_only_in_legacy_overlay_still_resolves_its_generation(
    tmp_path: Path,
) -> None:
    """An outcome written with ``coord link`` must still open its review.

    ``_load_outcomes`` selects the folded outcome from the union of the
    structure store and the legacy kanban overlay, but the generation lookup
    behind it used to re-derive the exact event from ``event_rows`` alone.
    Almost every provisional PASS on the fleet is written as
    ``coord link --key verdict``, which lands only in the overlay, so the
    lookup found nothing and the opener reported OPEN_REVIEW_EVIDENCE_BLOCKED
    for a card whose candidate evidence was fully present and hash-verified.
    """
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", writer="pi-codex-source")
    # The structure store holds no outcome event: the verdict, and the
    # candidate evidence bound to it, live only in the overlay.
    board.structure["a1b2c3d4"] = []

    assert board.open(1) == 1
    assert not any("OPEN_REVIEW_EVIDENCE_BLOCKED" in row for row in board.logs)


def test_outcome_mirrored_into_both_stores_is_not_seen_as_conflicting(
    tmp_path: Path,
) -> None:
    """A mutation mirrored into both stores applies once, not twice.

    The overlay is the post-cutover hot backup, so the same event legitimately
    appears in both places.  Counting it twice would read as two conflicting
    outcome identities and fail closed on a perfectly healthy card.
    """
    board = OpenerHarness(tmp_path)
    board.outcome("a1b2c3d4", writer="pi-codex-source")
    board.structure["a1b2c3d4"] = [dict(board.events["a1b2c3d4"][0])]

    assert board.open(1) == 1
