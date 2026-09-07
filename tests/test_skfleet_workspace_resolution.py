"""Fail-closed workspace resolution tests for fleet worker launches."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _resolve_workspace_root():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_workspace_root"
    )
    namespace = {"Path": Path}
    exec(compile(ast.Module([node], []), str(ROTATE), "exec"), namespace)
    return namespace["_resolve_workspace_root"]


def test_nested_checkout_is_selected_deterministically(tmp_path: Path) -> None:
    checkout = tmp_path / "skcapstone"
    checkout.mkdir()
    (checkout / ".git").mkdir()

    assert _resolve_workspace_root()(tmp_path) == str(checkout)


def test_empty_workspace_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly one Git checkout"):
        _resolve_workspace_root()(tmp_path)


def test_ambiguous_workspace_root_fails_closed(tmp_path: Path) -> None:
    for name in ("first", "second"):
        checkout = tmp_path / name
        checkout.mkdir()
        (checkout / ".git").write_text("gitdir: /tmp/example\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one Git checkout"):
        _resolve_workspace_root()(tmp_path)
