"""Cycle breaking removes the parent-to-child edge, never the child-to-parent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from break_dependency_cycles import find_cycles, plan_breaks  # noqa: E402


def test_find_cycles_detects_the_two_card_pathology():
    cycles = find_cycles({"leaf": ["parent"], "parent": ["leaf"]})
    assert len(cycles) == 1
    assert set(cycles[0]) == {"leaf", "parent"}


def test_find_cycles_ignores_a_clean_graph():
    assert find_cycles({"leaf": ["parent"], "parent": []}) == []


def test_plan_breaks_removes_the_parent_to_child_edge():
    """A parent must not depend on its own child. That is what closes the loop."""
    cycles = [["parent", "leaf"]]
    parents = {"leaf": "parent"}
    assert plan_breaks(cycles, parents) == [("parent", "leaf")]


def test_plan_breaks_leaves_the_child_to_parent_edge_intact():
    cycles = [["parent", "leaf"]]
    parents = {"leaf": "parent"}
    breaks = plan_breaks(cycles, parents)
    assert ("leaf", "parent") not in breaks
