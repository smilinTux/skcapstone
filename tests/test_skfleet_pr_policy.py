"""Regression tests for the fleet launcher's PR/dispatch policy constants.

This file pins policy-level distinctions in ``scripts/fleet/skfleet-rotate.py``
that look interchangeable at a glance but answer different governance
questions. The script is not importable (it is a hyphenated top-level
script), so every test here extracts the relevant constants or functions
straight from the source via ``ast`` and executes just those nodes in an
isolated namespace, following the pattern already used by
``tests/test_skfleet_claimability.py`` and ``tests/test_skfleet_lane_affinity.py``.

Later tasks in this plan append more tests here. Keep the extraction
helpers at the top of the file and add new test functions below the
existing ones so the file stays a single coherent source of truth for
PR/dispatch policy behaviour.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_category_matchers() -> dict[str, object]:
    """Extract the two category-matching regex constants from the source.

    ``_SENSITIVE_CATEGORY`` gates whether a card needs the
    ``dispatch-approved`` opt-in before it can be dispatched at all.
    ``_QWEN_UNSUITABLE`` gates whether the qwen lane specifically may take
    a card. They share a subject-matter prefix by design; this loader pulls
    both, unmodified, directly from the live source so a future edit that
    accidentally merges or aliases them is caught here rather than in
    production routing.
    """
    names = {"_SENSITIVE_CATEGORY", "_QWEN_UNSUITABLE"}
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
    ]
    found = {
        target.id for node in nodes for target in node.targets if isinstance(target, ast.Name)
    }
    assert found == names, f"expected {names}, found {found}"
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_both_category_matchers_exist() -> None:
    namespace = _load_category_matchers()
    assert isinstance(namespace["_SENSITIVE_CATEGORY"], re.Pattern)
    assert isinstance(namespace["_QWEN_UNSUITABLE"], re.Pattern)


def test_category_matchers_are_not_equivalent() -> None:
    """They must not be collapsed into one pattern: they answer different questions."""
    namespace = _load_category_matchers()
    sensitive = namespace["_SENSITIVE_CATEGORY"]
    qwen_unsuitable = namespace["_QWEN_UNSUITABLE"]

    assert sensitive.pattern != qwen_unsuitable.pattern

    # Both matchers agree that credential-sensitive cards need gating.
    assert sensitive.search("credential rotation") is not None
    assert qwen_unsuitable.search("credential rotation") is not None

    # Only the qwen-suitability matcher cares about schema/architecture work.
    # The dispatch-approved gate does not require sign-off for these terms.
    assert qwen_unsuitable.search("update the schema") is not None
    assert qwen_unsuitable.search("revise the architecture") is not None
    assert sensitive.search("update the schema") is None
    assert sensitive.search("revise the architecture") is None
