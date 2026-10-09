"""Regression: the Niobe cycle stays inside the 270-second wrapper deadline.

The dispatcher reserves 20 seconds for cleanup and the final CYCLE_RECEIPT,
stops launching at the monotonic inner deadline, and caches each logical
route's preflight (success or failure) so 13 lane-compatible rejected
candidates trigger the gateway preflight at most once per route per cycle.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

ROTATE = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def test_cycle_deadline_reserves_receipt_window() -> None:
    source = ROTATE.read_text()
    assert "_CYCLE_DEADLINE_RESERVE_S = 20" in source
    # Deployed 997b9795 uses the shared production cycle start and budget.
    assignment = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_cycle_deadline" for t in node.targets)
    )
    for policy, expected in [(None, 750), ({"enabled": True}, 220)]:
        namespace = {
            "PRODUCTION_POLICY": policy,
            "_cycle_started": 100,
            "_production_cycle_budget": 120,
            "_CYCLE_DEADLINE_RESERVE_S": 20,
            "time": SimpleNamespace(monotonic=lambda: 500),
        }
        exec(
            compile(ast.Module(body=[assignment], type_ignores=[]), str(ROTATE), "exec"), namespace
        )
        assert namespace["_cycle_deadline"] == expected
    # On reaching the inner deadline the loop stops launching and logs the
    # deferral so the final receipt still gets written before the wrapper
    # timeout.
    assert "CYCLE_DEADLINE_REACHED" in source
    assert "_deferred_ids" in source


def test_route_preflight_caches_rejected_routes_per_cycle() -> None:
    source = ROTATE.read_text()
    # Each logical route is resolved/preflighted once; a cached failure must
    # not re-invoke the gateway for later compatible candidates.
    assert "_route_preflight_cache = {}" in source
    assert "reason=cached-failure" in source
    assert (
        "resolve_and_preflight(\n                _GATEWAY_ENDPOINT,model,deadline=_cycle_deadline)"
        in source
    )


def test_route_preflight_cache_key_changes_with_gateway_revision() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_route_preflight_cache_key"
    )
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(body=[helper], type_ignores=[]), str(ROTATE), "exec"), namespace)
    key = namespace["_route_preflight_cache_key"]

    assert key("glm-5", {"capacity_revision": "old"}) == ("glm-5", "old")
    assert key("glm-5", {"capacity_revision": "new"}) == ("glm-5", "new")
    assert key("glm-5", {"capacity_revision": "old"}) != key(
        "glm-5", {"capacity_revision": "new"}
    )
    assert "_preflight_key = _route_preflight_cache_key(model, _review_route_snapshot)" in source
    assert "_route_preflight_cache[_preflight_key]" in source


def test_route_preflight_cannot_consume_receipt_reserve_or_reach_launch() -> None:
    source = ROTATE.read_text()
    probe = source.index("_route_preflight=resolve_and_preflight")
    post_probe_deadline = source.index("time.monotonic() >= _cycle_deadline", probe)
    workspace = source.index("default_workspace=os.path.join", probe)
    receipt = source.index('log(d,"CYCLE_RECEIPT|')

    assert probe < post_probe_deadline < workspace < receipt
    assert "CYCLE_DEADLINE_REACHED" in source[post_probe_deadline:workspace]


def test_cycle_receipt_remains_writable_after_deadline_stop() -> None:
    source = ROTATE.read_text()
    # The final receipt is written after the loop, proving a clean exit path
    # even when the deadline stopped the loop early.
    idx_deadline = source.index("CYCLE_DEADLINE_REACHED")
    idx_receipt = source.index('log(d,"CYCLE_RECEIPT|')
    assert idx_deadline < idx_receipt
