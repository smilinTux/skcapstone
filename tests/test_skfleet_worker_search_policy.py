"""Regression tests for bounded filesystem searches in fleet worker briefs."""

from __future__ import annotations

import ast
from pathlib import Path

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _search_instructions() -> str:
    """Load the generated search-policy fragment without running the fleet script."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_worker_search_instructions"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module([node], []), str(ROTATE), "exec"), namespace)
    return namespace["_worker_search_instructions"]()


def test_worker_searches_are_bounded_to_exact_authorized_roots() -> None:
    """Require generated briefs to reject broad host traversal explicitly."""
    instructions = _search_instructions()

    assert "exact authorized repository or evidence root" in instructions
    assert "Prefer rg or rg --files" in instructions
    assert "bounded filters, result limits, and timeouts" in instructions
    assert "find /, find /home" in instructions
    assert "equivalent broad traversal" in instructions
