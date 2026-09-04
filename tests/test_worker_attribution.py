"""Tests for worker identity attribution in review events (card 4c9d7a12)."""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _rotate_source() -> str:
    return ROTATE.read_text(encoding="utf-8")


def test_review_assignment_uses_reviewer_not_jarvis():
    """authorize_review_launch must receive the actual reviewer, not 'jarvis'."""
    src = _rotate_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_review_assignment":
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    func = call.func
                    if isinstance(func, ast.Name) and func.id == "authorize_review_launch":
                        for kw in call.keywords:
                            if kw.arg == "actor":
                                assert isinstance(
                                    kw.value, ast.Name
                                ), "actor must be a variable (the reviewer), not a literal"
                                assert (
                                    kw.value.id != "jarvis"
                                ), "actor must not be hardcoded 'jarvis'"
                                return
    pytest.fail("authorize_review_launch call with actor= not found in _review_assignment")


def test_launch_receipt_uses_worker_name_not_jarvis():
    """append_review_launch_receipt must receive the worker name, not 'jarvis'."""
    src = _rotate_source()
    # Find the pattern in the launch loop
    assert (
        "actor=name," in src or "actor=name" in src
    ), "launch receipt must use the actual worker name variable"
    # Ensure the old hardcoded pattern is gone from the receipt call
    # (it may still exist in authorize_review_launch which is now fixed)
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if "append_review_launch_receipt" in line:
            # Look at the next few lines for the actor= parameter
            context = "\n".join(lines[i : i + 6])
            if "actor=" in context:
                assert (
                    '"jarvis"' not in context or "actor=name" in context
                ), f"launch receipt at line {i+1} still hardcodes actor='jarvis'"


def test_no_jarvis_hardcoded_in_review_paths():
    """Broader check: no review-related function should hardcode jarvis as actor."""
    src = _rotate_source()
    tree = ast.parse(src)

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "jarvis":
            # Check if this literal is used as an actor= keyword
            # We can't easily walk backwards, so check the surrounding context
            pass

    # Simpler: count occurrences of actor="jarvis" in the source
    count = src.count('actor="jarvis"')
    assert count == 0, (
        f'found {count} hardcoded actor="jarvis" in skfleet-rotate.py; '
        "all actor fields must use the actual worker/reviewer identity"
    )
