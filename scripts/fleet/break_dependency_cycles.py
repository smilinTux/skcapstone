#!/usr/bin/env python3
"""Find and break the dependency cycles that make cards permanently unclosable.

Measured 2026-09-16: 23 cycles, essentially one pathology. A parent "Resolve or
quarantine" card depended on its own leaves while the leaves depended on it.

The break is always the PARENT-to-CHILD edge. A child legitimately depends on
its parent's outcome; a parent depending on its own child is what closes the
loop. Removing the wrong edge would orphan the child instead of freeing it.

Dry run by default. Pass --apply to write remove_dependency events.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PARENT_LABEL_PREFIX = "parent-"


def find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """Return one representative node list per distinct cycle.

    Standard DFS three-colour cycle detection: white (unseen), grey (on the
    current path), black (fully explored). A back-edge to a grey node closes
    a cycle; the cycle is the slice of the current path from that node on.
    Distinct cycles are deduplicated by their member set so that revisiting
    the same loop from a different start node does not double-count it.
    """
    cycles: list[list[str]] = []
    seen_signatures: set[frozenset[str]] = set()
    colour: dict[str, int] = {}

    def visit(node: str, path: list[str]) -> None:
        colour[node] = 1
        path.append(node)
        for nxt in edges.get(node, ()):
            if colour.get(nxt) == 1:
                cycle = path[path.index(nxt) :]
                signature = frozenset(cycle)
                if signature not in seen_signatures:
                    seen_signatures.add(signature)
                    cycles.append(list(cycle))
            elif colour.get(nxt, 0) == 0:
                visit(nxt, path)
        path.pop()
        colour[node] = 2

    for node in list(edges):
        if colour.get(node, 0) == 0:
            visit(node, [])
    return cycles


def plan_breaks(cycles: list[list[str]], parents: dict[str, str]) -> list[tuple[str, str]]:
    """Return the (card_id, dependency_id) edges to remove.

    ``parents`` maps a child card id to its parent card id. For every cycle,
    any child/parent pair that both appear in the cycle is broken on the
    parent-to-child edge (the parent's dependency on its own child), never
    the child-to-parent edge (the child's legitimate wait on its parent).
    """
    breaks: list[tuple[str, str]] = []
    for cycle in cycles:
        members = set(cycle)
        for child, parent in parents.items():
            if child in members and parent in members:
                edge = (parent, child)
                if edge not in breaks:
                    breaks.append(edge)
    return breaks


def _resolved(cycle: list[str], breaks: list[tuple[str, str]]) -> bool:
    """Return True if one of the planned breaks is this cycle's own closing edge.

    Node membership is not enough: a card can sit in two distinct cycles, and
    breaking an edge that resolves one of them says nothing about the other
    just because they share a node. The correct test is whether a planned
    break is an adjacent pair in this cycle's own ring, in either direction.
    """
    ring = list(cycle)
    adjacent: set[tuple[str, str]] = set()
    for i in range(len(ring)):
        a, b = ring[i], ring[(i + 1) % len(ring)]
        adjacent.add((a, b))
        adjacent.add((b, a))
    return any(tuple(brk) in adjacent for brk in breaks)


def unbroken_cycles(cycles: list[list[str]], breaks: list[tuple[str, str]]) -> list[list[str]]:
    """Return the cycles whose own closing edge was not among the planned breaks."""
    return [cycle for cycle in cycles if not _resolved(cycle, breaks)]


def _parent_from_labels(labels: list[str]) -> str | None:
    """Return the single parent-<id> label target, or None if there isn't one."""
    found = {
        label[len(PARENT_LABEL_PREFIX) :]
        for label in labels
        if isinstance(label, str)
        and label.lower().startswith(PARENT_LABEL_PREFIX)
        and label[len(PARENT_LABEL_PREFIX) :]
    }
    if len(found) == 1:
        return found.pop()
    return None


def _load_graph_via_skcoord(home: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read the folded, event-applied dependency graph through skcoord.CardStore.

    core.json alone can be stale: dependencies are added or removed by later
    append-only events, and CardStore.fold() is the code that already knows
    how to replay them. Preferring this path over a raw core.json read avoids
    acting on a graph that has since been fixed or has since gotten worse.
    """
    from skcoord.card_store import CardStore

    store = CardStore(home)
    edges: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    for card in store.list_cards(include_archived=True):
        edges[card.id] = list(card.dependencies)
        parent = _parent_from_labels(list(card.labels))
        if parent is not None:
            parents[card.id] = parent
    return edges, parents


def _load_graph_via_core_json(home: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Fallback reader: core.json only. May be stale, see load_graph()."""
    edges: dict[str, list[str]] = {}
    parents: dict[str, str] = {}
    cards_dir = home / "cards"
    if not cards_dir.is_dir():
        return edges, parents
    for card_dir in cards_dir.iterdir():
        core_path = card_dir / "core.json"
        if not core_path.exists():
            continue
        core = json.loads(core_path.read_text(encoding="utf-8"))
        edges[card_dir.name] = [str(x) for x in (core.get("dependencies") or [])]
        labels = [str(x) for x in (core.get("initial_labels") or [])]
        parent = _parent_from_labels(labels)
        if parent is not None:
            parents[card_dir.name] = parent
    return edges, parents


def load_graph(home: Path) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Read the dependency graph and the parent map from the CardStore.

    Prefers skcoord's folded view (accounts for dependency-changing events
    appended after card birth). Falls back to reading core.json directly if
    skcoord is not importable on this host, in which case the result may be
    stale and the caller is told so.
    """
    try:
        return _load_graph_via_skcoord(home)
    except ImportError:
        print("WARNING skcoord not importable; falling back to raw core.json reads")
        print("WARNING core.json alone can be stale: later events may have added or")
        print("WARNING removed dependencies since a card's birth record was written")
        return _load_graph_via_core_json(home)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", default=str(Path.home() / ".skcapstone"))
    parser.add_argument("--apply", action="store_true", help="write removals (default: dry run)")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    edges, parents = load_graph(home)
    cycles = find_cycles(edges)
    breaks = plan_breaks(cycles, parents)

    print(f"cycles found: {len(cycles)}")
    print(f"edges to remove: {len(breaks)}")
    for parent, child in breaks:
        print(f"  remove {parent} -> {child}")

    unbroken = unbroken_cycles(cycles, breaks)
    if unbroken:
        print(f"WARNING {len(unbroken)} cycles have no parent edge to break:")
        for cycle in unbroken:
            print(f"  {' -> '.join(cycle)}")

    if not args.apply:
        print("dry run. pass --apply to write remove_dependency events")
        return 0

    from skcoord.card_store import remove_dependency

    for parent, child in breaks:
        remove_dependency(home, parent, child, agent="cycle-breaker", reason="dependency-cycle")
        print(f"  removed {parent} -> {child}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
