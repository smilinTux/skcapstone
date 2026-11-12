"""The claim ceiling stops runaway re-dispatch using ledger claim events."""

from __future__ import annotations

import ast
import bisect
import collections
import datetime
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {
    "_claim_ceiling_hit",
    "_countable_claims",
    "_work_epochs",
    "_work_between",
    "_claim_amnesty_epoch",
    "_fold_key",
    "_ts_epoch",
}
CONSTANTS = {
    "_MAX_CLAIMS",
    "_AMNESTY_VALUE_RE",
    "_CLAIM_BOOKKEEPING",
    "_BOOKKEEPING_LINK_KEYS",
    "_CLAIM_CLOSING",
    "_REAP_WRITER",
}


def _load_ceiling_namespace(acts_result: collections.Counter) -> dict:
    """Extract the pure ceiling seam from the script without running it."""
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
        "os": os,
        "re": re,
        "bisect": bisect,
        "datetime": datetime,
        "collections": collections,
        "acts": lambda cid: acts_result,
        # No amnesty on the board: the ceiling must behave exactly as before.
        "event_rows": lambda cid: [],
        "_load_evidence_events": lambda: {},
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert FUNCTIONS <= namespace.keys(), "ceiling seam missing from script"
    return namespace


def test_runaway_card_hits_the_ceiling():
    """402 claims and no completion is the measured 06a95c23 shape."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 402, "release_claim": 8}))
    assert ns["_claim_ceiling_hit"]("06a95c23") is True


def test_healthy_card_does_not_hit_the_ceiling():
    """5a7d31ce completed on its first claim and must stay selectable."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 1, "complete": 1}))
    assert ns["_claim_ceiling_hit"]("5a7d31ce") is False


def test_completed_card_is_never_ceilinged():
    """A card that completed is finished, regardless of how many claims it took."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 99, "complete": 1}))
    assert ns["_claim_ceiling_hit"]("noisy") is False


def test_awaiting_gates_card_is_never_ceilinged():
    """A worker that finished and is waiting on another seat is not runaway."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 99, "await_gates": 1}))
    assert ns["_claim_ceiling_hit"]("waiting") is False


def test_ceiling_boundary_is_exclusive():
    """Exactly _MAX_CLAIMS is allowed; the next claim trips it."""
    ns = _load_ceiling_namespace(collections.Counter({"claim": 5}))
    assert ns["_claim_ceiling_hit"]("edge") is False
    ns = _load_ceiling_namespace(collections.Counter({"claim": 6}))
    assert ns["_claim_ceiling_hit"]("edge") is True


def _load_selector_namespace(*, ceiling_hit: bool, backoff: bool) -> dict:
    """Extract the pure _legacy_selector_decision seam with stubbed facts."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_legacy_selector_decision"
    ]
    namespace = {
        "excluded": set(),
        "_REVIEW_READBACK_BLOCKED": set(),
        "unclaimable": lambda cid: False,
        "itil_terminal": lambda cid: False,
        "lifecycle_state": lambda cid: "open",
        "awaiting_review": lambda cid: False,
        "outcome_lifecycle_bucket": lambda lifecycle, historical_review: "open",
        "blocked_backoff": lambda cid: backoff,
        "_claim_ceiling_hit": lambda cid: ceiling_hit,
        "terminal_review_verdict": lambda cid, core: False,
        "authoritative_claimability": lambda cid, core=None: {
            "claimable": True,
            "reason": "claimable",
            "title": "t",
            "labels": [],
            "core": core or {},
        },
        "json": __import__("json"),
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    assert "_legacy_selector_decision" in namespace, "selector seam missing from script"
    return namespace


def test_claim_ceiling_hit_is_reported_distinctly_from_ordinary_backoff(tmp_path):
    """The exclusion must be visible, not folded silently into 'backoff'.

    Fix 4: _claim_ceiling_hit is folded into blocked_backoff, so a
    ceiling-hit card and an ordinary backoff card looked identical in the
    selector's reason. Claim counts are monotonic, so a card excluded this
    way never recovers; that must not be indistinguishable from a card that
    will retry on its own.
    """
    core_path = tmp_path / "core.json"
    core_path.write_text("{}")
    ns = _load_selector_namespace(ceiling_hit=True, backoff=True)

    decision = ns["_legacy_selector_decision"]("deadbeef", str(core_path))

    assert decision == {"eligible": False, "reason": "claim_ceiling"}


def test_ordinary_backoff_without_ceiling_is_unchanged(tmp_path):
    """A card that is not ceiling-hit keeps reporting plain backoff."""
    core_path = tmp_path / "core.json"
    core_path.write_text("{}")
    ns = _load_selector_namespace(ceiling_hit=False, backoff=True)

    decision = ns["_legacy_selector_decision"]("deadbeef", str(core_path))

    assert decision == {"eligible": False, "reason": "backoff"}
