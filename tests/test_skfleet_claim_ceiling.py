"""The claim ceiling stops runaway re-dispatch using ledger claim events."""

from __future__ import annotations

import ast
import collections
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"

FUNCTIONS = {"_claim_ceiling_hit"}
CONSTANTS = {"_MAX_CLAIMS"}


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
    namespace = {"os": os, "collections": collections, "acts": lambda cid: acts_result}
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
