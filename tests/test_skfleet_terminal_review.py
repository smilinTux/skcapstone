"""Terminal independent-review verdicts must not be dispatched again."""

import ast
import re
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _load(labels, outcomes):
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "terminal_review_verdict"
    )
    namespace = {
        "re": re,
        "folded_labels": lambda cid, core: labels,
        "_load_outcomes": lambda: {"card": outcomes},
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SCRIPT), "exec"), namespace)
    return namespace["terminal_review_verdict"]("card", {})


def test_review_fail_is_terminal():
    assert _load(["review"], ("2026-09-01T14:28:05Z", "FAIL: evidence mismatch"))


def test_review_pass_is_terminal():
    assert _load(["review"], ("2026-09-01T14:28:05Z", "PASS"))


def test_source_pass_for_review_is_not_terminal():
    assert not _load(["repair"], ("2026-09-01T14:28:05Z", "PASS_FOR_REVIEW"))


def test_blocked_review_can_be_retried_after_change():
    assert not _load(["review"], ("2026-09-01T14:28:05Z", "BLOCKED: dependency"))
