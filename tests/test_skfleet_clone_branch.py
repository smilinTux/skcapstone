"""`git clone --branch` takes a name, never a full ref path.

Measured 2026-09-21 on chiap03 card d621aeec: the branch
`fix/d621aeec-reproducible-role-spec` existed on skgit and `ls-remote` found
it, while the clone reported it missing, so the card read as a dead binding
when the only fault was this argument.

    --branch refs/heads/fix/...  -> fatal: Remote branch ... not found
    --branch fix/...             -> clones cleanly
"""

import ast
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _fn():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_clone_branch_argument"
    ]
    assert body, "_clone_branch_argument not found"
    namespace: dict = {}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_clone_branch_argument"]


def test_a_heads_ref_is_reduced_to_the_branch_name():
    assert _fn()("refs/heads/fix/d621aeec-reproducible-role-spec") == (
        "fix/d621aeec-reproducible-role-spec"
    )


def test_a_tags_ref_is_reduced_to_the_tag_name():
    assert _fn()("refs/tags/v0.15.168") == "v0.15.168"


def test_a_short_name_is_left_alone():
    assert _fn()("main") == "main"
    assert _fn()("fix/something") == "fix/something"


def test_a_slash_heavy_branch_name_keeps_every_segment():
    """Only the prefix is removed, never an inner path segment."""
    assert _fn()("refs/heads/a/b/c") == "a/b/c"


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_does_not_raise(value):
    assert _fn()(value) == ""


def test_a_ref_that_merely_contains_the_prefix_is_untouched():
    """Only a LEADING prefix is stripped."""
    assert _fn()("feature/refs/heads/weird") == "feature/refs/heads/weird"


def test_the_clone_call_uses_the_helper():
    """Guards the wiring: the raw base_ref must not reach --branch."""
    source = ROTATE.read_text(encoding="utf-8")
    assert '"--branch", _clone_branch_argument(base_ref),' in source
    assert (
        '"--branch", base_ref,' not in source
    ), "the raw base_ref must never be passed to git clone --branch"
