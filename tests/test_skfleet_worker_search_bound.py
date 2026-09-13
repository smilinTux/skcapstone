"""Card f96b1fd2 [SKFLEET-WORKER-SEARCH-BOUNDING-RSI] regression + repair tests.

Criterion 2 regression (must FAIL before the repair, PASS after): the
generated worker instructions must forbid unbounded root/home searches.
The pre-repair brief (the top-level `_RAILS` invariant rails) carries no
SEARCH POLICY, so it permits `find /` and `find /home`. The repair splices
`search_policy_block()` into `_RAILS` and fail-closed gates the brief with
`enforce_search_policy`.

The test file mirrors the AST-extraction pattern used by
tests/test_skfleet_pi_tool_allowlist.py so it never has to import the
hyphenated script directly.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _extract_leg_rails_legacy() -> str:
    """Re-derive the pre-repair `_RAILS` string from the source.

    The top-level `_RAILS = (...)` is a parenthesised concatenation of string
    literals. We parse the source, find the first top-level `_RAILS`
    assignment, and evaluate only its first (base) string constant -- the
    part before any `search_policy_block()` splice. That base is exactly what
    the reviewer's generated brief contained before this repair.
    """
    source = ROTATE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_RAILS":
                    target = node
                    break
        if target is not None:
            break
    if target is None:
        pytest.skip("top-level _RAILS assignment not found")
    value = target.value
    # Pre-repair value shape: _RAILS = CONSTRAINS_CONST + MAIL_CALL + MORE_CONST
    #   i.e. BinOp(left=BinOp(left=CONSTRAINS_CONST, right=MAIL_CALL),
    #              right=MORE_CONST)
    # The base invariant-rails prefix is the innermost left constant.
    base_node = value
    if isinstance(base_node, ast.BinOp):
        inner = base_node.left
        if isinstance(inner, ast.BinOp):
            base_node = inner.left
        elif isinstance(inner, ast.Constant):
            base_node = inner
    base = ast.literal_eval(base_node)
    return base


def test_REGRESSION_legacy_brief_permits_unbounded_search() -> None:
    """Failing regression (criterion 2).

    Proves the pre-repair generated worker instructions did NOT forbid
    unbounded root/home searches: the base `_RAILS` has no SEARCH POLICY
    block, so a reviewer (or worker) is free to run `find /home` and
    `find /` -- the exact c2d84daf incident.
    """
    legacy = _extract_leg_rails_legacy()
    # The base rails never mention the search policy directives.
    assert "SEARCH POLICY" not in legacy
    assert "EXACT AUTHORIZED START PATH" not in legacy
    assert "rg --files" not in legacy
    # ...so unbounded searches are not constrained:
    assert "find /" not in legacy
    assert "find /home" not in legacy


def test_repair_policy_block_is_fail_closed() -> None:
    """The spliced search policy block satisfies its own validator."""
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from skcapstone.fleet.search_bound import (
        LIVE_EVIDENCE_SHA256,
        REQUIRED_DIRECTIVES,
        enforce_search_policy,
        search_policy_block,
    )
    block = search_policy_block()
    # Every required directive is present, so the gate cannot pass a policy-less brief.
    assert enforce_search_policy(block) is True
    for directive in REQUIRED_DIRECTIVES:
        assert directive in block
    # The live c2d84daf evidence SHA is preserved verbatim in the block.
    assert LIVE_EVIDENCE_SHA256 in block
    assert LIVE_EVIDENCE_SHA256 == "2d8828c3c2ee1c0a8a2b6bc9a9d4cfe4f6ec2743dc52f0fa440913c7389763b4"


def test_enforce_rejects_policy_less_brief() -> None:
    """A brief missing the search policy is rejected (fail-closed)."""
    import sys
    sys.path.insert(0, str(ROOT / "src"))
    from skcapstone.fleet.search_bound import enforce_search_policy

    # A brief with only the base rails (no search policy) must be rejected.
    assert enforce_search_policy(_extract_leg_rails_legacy()) is False
    # And a brief that is completely empty is rejected too.
    assert enforce_search_policy("") is False


def test_rotate_splices_policy_and_gates_brief() -> None:
    """The launcher both splices the block into _RAILS and gates the brief."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "from skcapstone.fleet.search_bound import" in source
    assert "_RAILS += search_policy_block()" in source
    assert "if not enforce_search_policy(brief):" in source
    assert "BRIEF_SEARCH_POLICY_MISSING" in source
