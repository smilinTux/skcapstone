"""Focused tests for the SKGit merge-on-PASS verdict join (card 87da4e8e).

Verifies the strict non-provisional PASS classifier, the parent-review
mapping join, and the fail-closed fast-forward integration sweep in
scripts/fleet/skfleet-rotate.py.
"""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

# Functions this module reuses or defines.
FUNCTIONS = {
    "_ts_epoch",
    "_skgit_pass_only_ok",
    "_skgit_review_passed",
    "_skgit_eligible_parents",
    "_skgit_repo_for_card",
    "_skgit_candidate_info",
    "_skgit_is_ancestor",
    "_skgit_tree_hash",
    "_skgit_repo_url",
    "_skgit_integrate_one",
    "_skgit_merge_on_pass",
    "_skgit_remote_head",
    "_skgit_git",
}
CONSTANTS = {
    "_SKGIT_PASS_ONLY_RE",
    "_SKGIT_REPOS",
    "_SKGIT_HOST",
    "_SKGIT_PORT",
    "_EVID_DIR",
    "CARDS",
}


def _load_namespace() -> dict[str, object]:
    """Extract the merge-on-PASS seam without running the whole launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if names & CONSTANTS:
                nodes.append(node)
    namespace = {
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
        "re": re,
        "HOME": str(Path.home()),
        "HOST": "chiap01",
        "d": Path.home() / ".skcapstone" / "evidence" / "fleet-rotation" / "20260831T000000Z",
        "log": lambda d_, m_: None,
    }
    namespace["subprocess"] = subprocess
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= set(namespace.keys())
    return namespace


class SkGitHarness:
    """Mutable board model for the merge-on-PASS sweep."""

    def __init__(self, tmp_path: Path) -> None:
        self.ns = _load_namespace()
        self.cards = tmp_path / "cards"
        self.cards.mkdir()
        self.events = tmp_path / "card_events"
        self.events.mkdir()
        self.outcomes: dict[str, tuple[str, str]] = {}
        self.lifecycle: dict[str, str] = {}
        self.parent_reviews: dict[str, set] = {}
        # Wire the seam to this harness.
        self.ns.update(
            {
                "CARDS": str(self.cards),
                "_EVID_DIR": str(self.events),
                "_load_outcomes": lambda: self.outcomes,
                "_reviews_by_parent": lambda: self.parent_reviews,
                "lifecycle_state": lambda cid: self.lifecycle.get(cid, "open"),
            }
        )

    def card(self, cid: str, title: str, tags: tuple[str, ...]) -> None:
        path = self.cards / cid
        path.mkdir(exist_ok=True)
        (path / "core.json").write_text(
            json.dumps({"id": cid, "title": title, "tags": list(tags)}),
            encoding="utf-8",
        )

    def outcome(self, cid: str, stamp: str, value: str) -> None:
        self.outcomes[cid] = (stamp, value)

    def review(self, parent: str, review_id: str) -> None:
        self.parent_reviews.setdefault(parent, set()).add(review_id)

    def state(self, cid: str, s: str) -> None:
        self.lifecycle[cid] = s

    def evidence_link(
        self, cid: str, key: str, value: str, stamp: str = "2026-08-30T00:00:00Z"
    ) -> None:
        event = {
            "ts": stamp,
            "action": "link",
            "card_id": cid,
            "link_key": key,
            "link_value": value,
            "writer": "test",
            "event_id": f"{cid}-{key}",
        }
        fname = "test.jsonl"
        path = self.events / fname
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    def merge(self, dry: bool = False) -> list:
        return self.ns["_skgit_merge_on_pass"](dry=dry)


@pytest.fixture
def board(tmp_path: Path) -> SkGitHarness:
    return SkGitHarness(tmp_path)


def _make_eligible(board: SkGitHarness) -> None:
    # Producer card with a non-provisional PASS and a completed PASS review.
    board.card("aaaa0001", "[SKGIT] reviewed SKLegal CI candidate", ("sklegal", "skgit"))
    board.card(
        "bbbb0002",
        "[REVIEW] Review provisional outcome for aaaa0001",
        ("review", "parent-aaaa0001"),
    )
    board.outcome("aaaa0001", "2026-08-30T10:00:00Z", "PASS")
    board.outcome("bbbb0002", "2026-08-30T11:00:00Z", "PASS")
    board.review("aaaa0001", "bbbb0002")
    board.state("bbbb0002", "complete")
    board.state("aaaa0001", "open")
    board.evidence_link("aaaa0001", "branch", "candidate/aaaa0001-ci")
    board.evidence_link("aaaa0001", "candidate", "c" * 40)
    board.evidence_link("aaaa0001", "tree", "d" * 40)


def test_pass_only_ok_accepts_exact_pass(board: SkGitHarness) -> None:
    f = board.ns["_skgit_pass_only_ok"]
    assert f("PASS")
    assert f("PASS|detail here")
    assert f("pass")
    assert not f("PASS_FOR_REVIEW")
    assert not f("PASS_READY_X")
    assert not f("FAIL")
    assert not f("BLOCKED")
    assert not f(None)
    assert not f("")


def test_provisional_pass_does_not_integrate(board: SkGitHarness) -> None:
    _make_eligible(board)
    # Overwrite the producer outcome to a provisional variant.
    board.outcome("aaaa0001", "2026-08-31T00:00:00Z", "PASS_FOR_REVIEW")
    decisions = board.merge(dry=True)
    # The provisional producer is excluded from the eligible join, so the
    # sweep makes no integration decision for it. A provisional PASS never
    # triggers integration.
    assert decisions == []
    assert "aaaa0001" not in [p for p, _ in board.ns["_skgit_eligible_parents"]()]


def test_eligible_parents_joins_review(board: SkGitHarness) -> None:
    _make_eligible(board)
    eligible = board.ns["_skgit_eligible_parents"]()
    assert ("aaaa0001", "bbbb0002") in eligible


def test_review_not_complete_excludes_parent(board: SkGitHarness) -> None:
    _make_eligible(board)
    board.state("bbbb0002", "doing")
    eligible = board.ns["_skgit_eligible_parents"]()
    assert not any(p == "aaaa0001" for p, _ in eligible)


def test_review_verdict_must_be_exact_pass(board: SkGitHarness) -> None:
    _make_eligible(board)
    board.outcome("bbbb0002", "2026-08-30T12:00:00Z", "PASS_FOR_REVIEW")
    eligible = board.ns["_skgit_eligible_parents"]()
    assert not any(p == "aaaa0001" for p, _ in eligible)


def test_repo_mapping(board: SkGitHarness) -> None:
    board.card("aaaa0001", "[SKGIT] reviewed SKLegal CI candidate", ("sklegal", "skgit"))
    assert board.ns["_skgit_repo_for_card"]("aaaa0001") == "SKLegal"
    board.card("cccc0003", "[HAM] hammer adapter", ("hammertime",))
    assert board.ns["_skgit_repo_for_card"]("cccc0003") == "HammerTime"
    board.card("dddd0004", "unrelated card", ("skdashboard",))
    assert board.ns["_skgit_repo_for_card"]("dddd0004") is None


def test_integrate_refused_on_not_fast_forward(board: SkGitHarness, monkeypatch) -> None:
    _make_eligible(board)
    # Stub the git helpers to simulate a diverged candidate (not a descendant).
    board.ns["_skgit_is_ancestor"] = lambda a, d_, dir_: False
    board.ns["_skgit_remote_head"] = lambda repo: ("a" * 40, "")
    decision = board.ns["_skgit_integrate_one"]("aaaa0001", "SKLegal", dry=True)
    assert decision["status"] == "REFUSED"
    assert decision["reason"] == "not_fast_forward"


def test_integrate_refused_on_tree_mismatch(board: SkGitHarness) -> None:
    _make_eligible(board)
    board.ns["_skgit_is_ancestor"] = lambda a, d_, dir_: True
    board.ns["_skgit_remote_head"] = lambda repo: ("a" * 40, "")
    board.ns["_skgit_tree_hash"] = lambda dir_, commit: ("e" * 40)
    decision = board.ns["_skgit_integrate_one"]("aaaa0001", "SKLegal", dry=True)
    assert decision["status"] == "REFUSED"
    assert decision["reason"] == "tree_mismatch"
    assert decision["final_tree"] == "e" * 40
    assert decision["reviewed_tree"] == "d" * 40


def test_integrate_already_integrated_idempotent(board: SkGitHarness) -> None:
    _make_eligible(board)
    # Remote main already equals the candidate commit: no-op, idempotent.
    board.ns["_skgit_remote_head"] = lambda repo: ("c" * 40, "")
    board.ns["_skgit_is_ancestor"] = lambda a, d_, dir_: True
    decision = board.ns["_skgit_integrate_one"]("aaaa0001", "SKLegal", dry=False)
    assert decision["status"] == "ALREADY_INTEGRATED"


def test_integrate_success_path(board: SkGitHarness) -> None:
    _make_eligible(board)
    # Simulate: base is "a"*40, candidate is "c"*40, tree "d"*40.
    # The initial remote head readback returns base "a"*40, the post-push
    # readback returns the candidate "c"*40.
    heads = [("a" * 40, ""), ("c" * 40, "")]
    calls = {"n": 0}

    def _fake_remote_head(repo):
        idx = min(calls["n"], len(heads) - 1)
        calls["n"] += 1
        return heads[idx]

    board.ns["_skgit_remote_head"] = _fake_remote_head
    board.ns["_skgit_is_ancestor"] = lambda a, d_, dir_: True
    board.ns["_skgit_tree_hash"] = lambda dir_, commit: "d" * 40
    board.ns["_skgit_git"] = lambda *args, timeout=120: (0, "")
    decision = board.ns["_skgit_integrate_one"]("aaaa0001", "SKLegal", dry=False)
    assert decision["status"] == "INTEGRATED"
    assert decision["candidate"] == "c" * 40
    assert decision["base"] == "a" * 40
    assert decision["final_main"] == "c" * 40


def test_sweep_no_repository_change_out_of_scope(board: SkGitHarness) -> None:
    # A card that is not SKLegal/HammerTime is out of scope and not integrated.
    board.card("eeee0005", "[SKGIT] skdashboard candidate", ("skdashboard", "skgit"))
    board.outcome("eeee0005", "2026-08-30T09:00:00Z", "PASS")
    board.card("ffff0006", "[REVIEW] Review for eeee0005", ("review", "parent-eeee0005"))
    board.outcome("ffff0006", "2026-08-30T09:30:00Z", "PASS")
    board.review("eeee0005", "ffff0006")
    board.state("ffff0006", "complete")
    board.state("eeee0005", "open")
    board.evidence_link("eeee0005", "branch", "candidate/eeee0005")
    board.evidence_link("eeee0005", "candidate", "1" * 40)
    board.ns["_skgit_remote_head"] = lambda repo: ("2" * 40, "")
    board.ns["_skgit_is_ancestor"] = lambda a, d_, dir_: True
    board.ns["_skgit_tree_hash"] = lambda dir_, commit: "3" * 40
    board.ns["_skgit_git"] = lambda *args, timeout=120: (0, "")
    decisions = board.merge(dry=True)
    by_parent = {x["parent"]: x for x in decisions}
    assert by_parent["eeee0005"]["status"] == "OUT_OF_SCOPE"
    assert by_parent["eeee0005"]["reason"] == "not_sklegal_or_hammertime"
