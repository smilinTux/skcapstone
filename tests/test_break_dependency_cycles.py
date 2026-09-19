"""Cycle breaking removes the parent-to-child edge, never the child-to-parent."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from break_dependency_cycles import find_cycles, plan_breaks, unbroken_cycles  # noqa: E402


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


def test_unbroken_cycles_uses_edge_membership_not_node_membership():
    """A card that sits in two cycles must not mask a genuinely unbroken one.

    A shares a node with both cycles. The A-B cycle is resolved by breaking
    A-B, but that must not be read as also resolving the separate A-C cycle
    just because A appears in the resolved break.
    """
    edges = {"A": ["B", "C"], "B": ["A"], "C": ["A"]}
    parents = {"B": "A"}
    cycles = find_cycles(edges)
    breaks = plan_breaks(cycles, parents)
    unresolved = [set(cycle) for cycle in unbroken_cycles(cycles, breaks)]
    assert {"A", "C"} in unresolved


def test_unbroken_cycles_resolves_a_three_node_transitive_cycle():
    """A 3-node transitive cycle whose closing edge was broken is resolved."""
    edges = {"A": ["B"], "B": ["C"], "C": ["A"]}
    parents = {"B": "A"}
    cycles = find_cycles(edges)
    breaks = plan_breaks(cycles, parents)
    assert unbroken_cycles(cycles, breaks) == []
