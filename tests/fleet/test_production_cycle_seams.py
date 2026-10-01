"""Exercise admission before an expensive route completion probe."""

import ast
from pathlib import Path

from skcapstone.fleet import production_test_profile


def test_unsupported_contract_cannot_reach_route_probe(tmp_path, monkeypatch):
    script = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(script.read_text())
    loop = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.For) and "_pick_index" in ast.unparse(node.target)
    )

    def contains(node, name):
        return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))

    gate = next(node for node in loop.body if contains(node, "test_preflight"))
    route = next(node for node in loop.body if contains(node, "resolve_and_preflight"))
    calls = []

    def refuse(*args):
        calls.append("contract")
        raise ValueError("required-test-profile-unqualified")

    monkeypatch.setattr(production_test_profile, "preflight", refuse)
    ordered = sorted(
        [(gate.lineno, gate), (route.lineno, ast.parse("calls.append('probe')").body[0])]
    )
    loop.body = [node for _, node in ordered]
    loop.target = ast.Name(id="item", ctx=ast.Store())
    loop.iter = ast.parse("[1]", mode="eval").body
    loop.orelse = []
    namespace = {
        "calls": calls,
        "PRODUCTION_POLICY": {"active": True},
        "Path": Path,
        "HOME": str(tmp_path),
        "HOST": "chiap08",
        "cid": "abc12345",
        "d": None,
        "fresh_claimability": {"core": {}, "labels": ["source-only"]},
        "_governed_review_metadata": lambda *args: None,
        "log": lambda *args: calls.append("withheld"),
        "_record_workspace_cooldown": lambda *args: calls.append("cooldown"),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
            str(script),
            "exec",
        ),
        namespace,
    )
    assert calls == ["contract", "withheld", "cooldown"]
