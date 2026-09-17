"""A worker that satisfied its criteria is finished, not re-dispatchable."""

from __future__ import annotations

import ast
import collections
import datetime
import glob
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(names, constants):
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
        elif isinstance(node, ast.Assign):
            ids = {t.id for t in node.targets if isinstance(t, ast.Name)}
            if ids & constants:
                nodes.append(node)
    ns = {
        "collections": collections,
        "datetime": datetime,
        "glob": glob,
        "json": json,
        "os": os,
        "re": re,
    }
    exec(compile(ast.Module(nodes, type_ignores=[]), str(ROTATE), "exec"), ns)
    return ns


def test_await_gates_event_sets_the_folded_flag():
    ns = _load({"_fold_claimability"}, set())
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    rows = [{"action": "claim", "owner": "w1"}, {"action": "await_gates"}]
    state = ns["_fold_claimability"](core, rows)
    assert state["awaiting_gates"] is True


def test_no_await_gates_event_leaves_the_flag_false():
    ns = _load({"_fold_claimability"}, set())
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    state = ns["_fold_claimability"](core, [{"action": "claim", "owner": "w1"}])
    assert state["awaiting_gates"] is False


def test_reopen_clears_awaiting_gates():
    """A reopened card is workable again; the flag must not be sticky."""
    ns = _load({"_fold_claimability"}, {"_COLUMNS"})
    core = {"title": "t", "dependencies": [], "initial_labels": []}
    rows = [{"action": "await_gates"}, {"action": "reopen"}]
    state = ns["_fold_claimability"](core, rows)
    assert state["awaiting_gates"] is False


def test_awaiting_gates_card_is_not_claimable():
    ns = _load({"_claimability_reason", "_coord_task_claimable", "non_implementation", "host_pin"}, set())
    state = {
        "title": "t",
        "description": "",
        "acceptance_criteria": [],
        "links": {},
        "labels": [],
        "dependencies": [],
        "owner": "",
        "status": "doing",
        "voided": False,
        "archived": False,
        "awaiting_gates": True,
    }
    assert ns["_claimability_reason"]({"kind": "task"}, state) == "awaiting-gates"


# --- Task 6: refuse cards whose criteria the worker cannot satisfy ---------
#
# The full state fold in _claimability_reason walks past awaiting-gates into
# review-marker and host-pin checks even for a plain claimable card, so the
# hand-built state dict needs review_seen/review_markers too, not just the
# description/acceptance_criteria/links the awaiting-gates test above already
# needed. host_pin() also reads the module-level KNOWN_HOSTS global, whose
# real definition chains into _resolve_rotation_hosts()/_estate_rotation_hosts()
# (environment and filesystem reads) -- extracting that chain would give the
# test real side effects for a value the gate logic never inspects (our test
# titles never name a host, so host_pin always returns None). _load_gate()
# extracts the static, side-effect-free globals the real function needs
# (including HOST, harmlessly just os.uname().nodename) and stubs KNOWN_HOSTS
# to an empty tuple afterward so host_pin's internal lookup resolves.
BASE_STATE = {
    "title": "t",
    "description": "",
    "acceptance_criteria": [],
    "links": {},
    "labels": [],
    "dependencies": [],
    "owner": "",
    "status": "ready",
    "voided": False,
    "archived": False,
    "awaiting_gates": False,
    "review_seen": False,
    "review_markers": {},
}
HELPERS = {"_claimability_reason", "_coord_task_claimable", "non_implementation", "host_pin"}
GATE_CONSTANTS = {
    "_GATE_LANGUAGE_RE",
    "_NOT_CLAIMABLE",
    "_SENSITIVE_CATEGORY",
    "_CATEGORY_OPT_IN",
    "_NON_IMPLEMENTATION_LABELS",
    "HOST",
}


def _load_gate():
    ns = _load(HELPERS, GATE_CONSTANTS)
    ns.setdefault("KNOWN_HOSTS", ())
    return ns


def test_v2_card_with_reviewer_criterion_is_refused():
    """The measured 06a95c23 criterion: AC4 named a reviewer's verdict."""
    ns = _load_gate()
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": [
            "Independent review PASS on the successor commit before merge"
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "criteria-not-satisfiable"


def test_v2_card_with_self_satisfiable_criteria_is_claimable():
    """5a7d31ce's shape: every criterion falsifiable by a test."""
    ns = _load_gate()
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": [
            "Add a deterministic fixture reproducing e125b710",
            "Pass focused and full scheduler tests, Ruff, formatting and compile",
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"


def test_legacy_card_with_reviewer_criterion_is_untouched():
    """spec_version absent means v1. The new gate must not apply."""
    ns = _load_gate()
    core = {
        "kind": "task",
        "acceptance_criteria": [
            "Independent review PASS on the successor commit before merge"
        ],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"


def test_gate_language_in_exit_gates_is_fine():
    """Gate language belongs in exit_gates. Only acceptance_criteria is checked."""
    ns = _load_gate()
    core = {
        "kind": "task",
        "spec_version": 2,
        "acceptance_criteria": ["Tests pass"],
        "exit_gates": [{"gate": "independent-review", "owner": "seraph"}],
    }
    assert ns["_claimability_reason"](core, dict(BASE_STATE)) == "claimable"
