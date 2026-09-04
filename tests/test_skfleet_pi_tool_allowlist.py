"""Regression tests for the bounded Pi fleet worker tool surface."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"
PI = shutil.which("pi")


def _pi_version() -> str:
    """Report the installed Pi version, for the skip reason below.

    Returns:
        The version string Pi prints, or "unknown" when it cannot be read.
    """
    if PI is None:
        return "unknown"
    try:
        result = subprocess.run(
            [PI, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except Exception:
        return "unknown"
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped and stripped[0].isdigit():
            return stripped
    return "unknown"


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


@pytest.mark.skipif(PI is None, reason="Pi executable is not installed")
def test_pi_denies_a_direct_mcp_tool_and_measures_schema_bytes(tmp_path: Path) -> None:
    """Exercise Pi itself and print reproducible before/after measurements."""
    assert PI is not None
    extension = tmp_path / "probe.ts"
    extension.write_text(
        textwrap.dedent("""
            import { writeFileSync } from "node:fs";
            export default function probe(pi: any) {
              pi.on("session_start", async () => {
                const active = new Set(pi.getActiveTools());
                const tools = pi.getAllTools().filter((tool: any) => active.has(tool.name));
                const schemas = tools.map(({name, description, parameters}: any) =>
                  ({name, description, parameters}));
                const directSchemas = schemas.filter(({name}: any) =>
                  /^(skcapstone|skcomms|skmemory)_/.test(name));
                writeFileSync(process.env.PI_TOOL_PROBE_OUT!, JSON.stringify({
                  names: tools.map((tool: any) => tool.name).sort(),
                  count: tools.length,
                  serialized_bytes: Buffer.byteLength(JSON.stringify(schemas)),
                  direct_count: directSchemas.length,
                  direct_serialized_bytes: Buffer.byteLength(JSON.stringify(directSchemas)),
                }) + "\\n");
              });
            }
            """),
        encoding="utf-8",
    )

    def measure(tools: str | None) -> dict[str, object]:
        output = tmp_path / ("allowlisted.json" if tools else "baseline.json")
        command = [
            str(PI),
            "--mode",
            "rpc",
            "--offline",
            "--no-session",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--extension",
            str(extension),
        ]
        if tools:
            command.extend(("--tools", tools))
        env = os.environ | {"PI_TOOL_PROBE_OUT": str(output)}
        subprocess.run(
            command,
            input='{"type":"get_state"}\n',
            text=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=True,
        )
        return json.loads(output.read_text(encoding="utf-8"))

    baseline = measure(None)
    if baseline["direct_count"] == 0:
        pytest.skip(
            "This Pi harness publishes no direct MCP tool schemas to a session "
            f"(pi {_pi_version()}, unrestricted surface = {baseline['names']}, "
            "direct_count=0), so there is no catalog for the allowlist to deny and "
            "nothing to measure. The assertions below are a measurement of a live "
            "Pi whose MCP servers are attached before session_start; they are not a "
            "property of skfleet-rotate.py, which is covered without Pi by "
            "test_ordinary_worker_gets_only_the_seven_native_coding_tools, "
            "test_mcp_access_is_one_explicit_proxy_tool_not_the_direct_catalog and "
            "test_launcher_passes_the_exact_allowlist_to_pi."
        )
    allowlisted = measure("read,bash,edit,write,grep,find,ls")
    direct_tool = "skcapstone_coord_status"

    assert direct_tool in baseline["names"]
    assert direct_tool not in allowlisted["names"]
    assert baseline["direct_count"] == 162
    assert allowlisted["direct_count"] == 0
    assert allowlisted["names"] == ["bash", "edit", "find", "grep", "ls", "read", "write"]
    assert allowlisted["count"] == 7
    assert allowlisted["serialized_bytes"] < baseline["serialized_bytes"]
    summary = {
        key: {field: value for field, value in result.items() if field != "names"}
        for key, result in (("baseline", baseline), ("allowlisted", allowlisted))
    }
    print(json.dumps(summary, sort_keys=True))
