"""Fleet workers preserve job size as provider-neutral SKGateway routes."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _helpers() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {"_GLM_SIZE_RE", "_LOGICAL_ROUTES"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id in wanted for target in node.targets
            )
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "_logical_route_for")
    ]
    namespace: dict[str, object] = {"re": re}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


@pytest.mark.parametrize(
    ("size", "route"),
    [("S", "sk-s"), ("M", "sk-m"), ("L", "sk-l"), ("XL", "sk-xl")],
)
def test_tshirt_size_maps_only_to_logical_gateway_route(size: str, route: str) -> None:
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": f"[CARD][{size}] Work"}) == route


@pytest.mark.parametrize(
    "title",
    ["[CARD] Missing size", "[CARD][S][M] Ambiguous size", ""],
)
def test_missing_or_ambiguous_size_fails_closed(title: str) -> None:
    helper = _helpers()["_logical_route_for"]
    assert helper({"title": title}) is None


def test_launch_never_replaces_logical_route_with_selected_member() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    assert 'model=str(_selected_route["model_or_bucket"])' not in source
    assert '"provider":"skgateway"' in source
    assert '"logical_route":model' in source
