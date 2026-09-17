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
