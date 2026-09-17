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


def _load_pr_required() -> dict[str, object]:
    """Extract ``_SENSITIVE_CATEGORY`` and ``pr_required`` from the source.

    ``pr_required`` reads the raw ``core.json`` dict directly (never through
    ``CardCore``/``CardStore.fold``: a model read silently drops fields on a
    node running an older skcoord), so this loader pulls both the regex it
    depends on and the function itself, unmodified, straight from the live
    source and executes them together in one namespace so the function's
    module-level lookup of ``_SENSITIVE_CATEGORY`` resolves correctly.
    """
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    assign_node = None
    func_node = None
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "_SENSITIVE_CATEGORY"
            for target in node.targets
        ):
            assign_node = node
        if isinstance(node, ast.FunctionDef) and node.name == "pr_required":
            func_node = node
    assert assign_node is not None, "_SENSITIVE_CATEGORY not found"
    assert func_node is not None, "pr_required not found"
    namespace: dict[str, object] = {"re": re}
    module = ast.Module(body=[assign_node, func_node], type_ignores=[])
    exec(compile(module, str(ROTATE), "exec"), namespace)
    return namespace


def test_sensitive_title_requires_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": "rotate the deploy key"}) is True


def test_ordinary_title_does_not_require_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": "fix a typo in the README"}) is False


def test_sensitive_tag_requires_pr() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    core = {"title": "fix a typo in the README", "initial_labels": ["migration"]}
    assert pr_required(core) is True


def test_missing_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({}) is False


def test_empty_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": ""}) is False


def test_non_string_title_does_not_raise() -> None:
    namespace = _load_pr_required()
    pr_required = namespace["pr_required"]
    assert pr_required({"title": None}) is False
    assert pr_required({"title": 12345}) is False
