"""Check SOP capacity evidence using isolated statements from the real dispatcher."""

import ast
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts/fleet/skfleet-rotate.py"
tree = ast.parse(DISPATCHER.read_text())
helper = next(
    node
    for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "_required_lane_target"
)
capacity = next(
    node
    for node in tree.body
    if isinstance(node, ast.If)
    and ast.unparse(node.test) == "PRODUCTION_POLICY"
    and any(isinstance(child, ast.Name) and child.id == "MAX_LAUNCH" for child in ast.walk(node))
)
code = compile(ast.Module(body=[helper, capacity], type_ignores=[]), str(DISPATCHER), "exec")
for policy, env, expected in (
    ({"capacity_authority": "skgateway"}, {}, (17, 17, 17, 0, 17)),
    (None, {"SKFLEET_TARGET": "2", "SKFLEET_GLM_TARGET": "3"}, (2, 3, 6, 0, 11)),
):
    namespace = {
        "os": SimpleNamespace(environ=env),
        "PRODUCTION_POLICY": policy,
        "_SCAN_BUDGET": 17,
    }
    exec(code, namespace)
    actual = tuple(
        namespace[key]
        for key in ("TARGET", "GLM_TARGET", "QWEN_TARGET", "KIMI_TARGET", "MAX_LAUNCH")
    )
    assert actual == expected, (actual, expected)

source = ROOT / "src/skcapstone/fleet/production_dispatch.py"
lanes = next(
    node
    for node in ast.parse(source.read_text()).body
    if isinstance(node, ast.FunctionDef) and node.name == "production_lanes"
)
namespace = {"provider_family": lambda name: name}
exec(compile(ast.Module(body=[lanes], type_ignores=[]), str(source), "exec"), namespace)
policy = {
    "lanes": {
        name: {"enabled": name != "kimi"} for name in ("codex", "glm", "deepseek", "qwen", "kimi")
    }
}
assert {lane["name"]: lane["target"] for lane in namespace["production_lanes"](policy, 17)} == {
    "codex": 17,
    "glm": 17,
    "deepseek": 17,
    "qwen": 17,
    "kimi": 0,
    "escalate": 17,
}
assert any(
    isinstance(node, ast.Assign)
    and any(isinstance(target, ast.Name) and target.id == "LANES" for target in node.targets)
    and ast.unparse(node.value) == "production_lanes(PRODUCTION_POLICY, _SCAN_BUDGET)"
    for node in ast.walk(tree)
)
policy_source = (ROOT / "src/skcapstone/fleet/production_policy.py").read_text()
assert 'value["capacity_authority"] != "skgateway"' in policy_source
print("SOP capacity contract: deployed gateway policy and legacy fallback verified")
