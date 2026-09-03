"""Regression tests for the bounded Pi fleet worker tool surface."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_tool_policy() -> dict[str, object]:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    names = {"PI_NATIVE_TOOLS", "PI_MCP_PROXY_LABEL"}
    body = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "pi_tool_allowlist")
    ]
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body, []), str(ROTATE), "exec"), namespace)
    return namespace


def test_ordinary_worker_gets_only_the_seven_native_coding_tools() -> None:
    policy = _load_tool_policy()

    assert policy["pi_tool_allowlist"]([]) == "read,bash,edit,write,grep,find,ls"


def test_mcp_access_is_one_explicit_proxy_tool_not_the_direct_catalog() -> None:
    policy = _load_tool_policy()

    tools = policy["pi_tool_allowlist"](["MCP-REQUIRED"]).split(",")

    assert tools == ["read", "bash", "edit", "write", "grep", "find", "ls", "mcp"]
    assert not any(tool.startswith("skcapstone_") for tool in tools)
    assert len(tools) <= 20


def test_launcher_passes_the_exact_allowlist_to_pi() -> None:
    source = ROTATE.read_text(encoding="utf-8")

    assert "pi_tools=pi_tool_allowlist(_labels)" in source
    assert "--thinking off --tools %s" in source
