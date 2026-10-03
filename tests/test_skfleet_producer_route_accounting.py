"""Regression for worker lane reservations against the served provider."""

import ast
from pathlib import Path

ROTATE = Path(__file__).resolve().parents[1] / "scripts/fleet/skfleet-rotate.py"


def _route_functions():
    """Load the pure route helpers without starting the fleet script."""
    source = ROTATE.read_text(encoding="utf-8")
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_routes_in_lane", "_producer_routes_for"}
    ]
    routes = [
        {"logical_route": "kimi-for-coding-highspeed", "capacity_domain": "kimi-for-coding"},
        {"logical_route": "sk-codex-mid", "capacity_domain": "codex"},
        {"logical_route": "sk-glm-l", "capacity_domain": "zai"},
        {"logical_route": "qwen3.8", "capacity_domain": "chiap08-qwen38"},
    ]
    namespace = {
        "_CAPACITY_DOMAINS": {
            "codex": ("codex",),
            "glm": ("zai",),
            "qwen": ("chiap08-qwen38",),
            "kimi": ("kimi-for-coding",),
        },
        "_size_class_for": lambda _core, _labels: "S",
        "_review_route_ambiguous": False,
        "_review_route_snapshot": {"routes": routes},
        "_review_route_occupancy": {},
        "eligible_gateway_routes": lambda snapshot, *_args: snapshot["routes"],
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def test_glm_worker_reserves_zai_even_when_kimi_route_sorts_first():
    """The selected worker lane determines the reserved capacity domain."""
    helpers = _route_functions()
    select = helpers["_producer_routes_for"]
    assert [route["capacity_domain"] for route in select({}, [], "glm")] == ["zai"]
    assert [route["capacity_domain"] for route in select({}, [], "codex")] == ["codex"]
    assert [route["capacity_domain"] for route in select({}, [], "qwen")] == ["chiap08-qwen38"]
    assert len(select({}, [], None)) == 4


def test_exact_model_launch_filters_routes_before_reservation():
    """An ordinary card must use its actual lane when reserving capacity."""
    source = ROTATE.read_text(encoding="utf-8")
    assert '_LANE["name"] if model != _bucket else None' in source
    assert 'if model != _bucket:\n            _routes=_routes_in_lane(_routes,"codex")' in source
