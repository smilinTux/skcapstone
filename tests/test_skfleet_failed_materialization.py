"""A failed clone must not block its card forever.

Materialization REUSES an existing workspace directory rather than cloning, so
a directory left behind by a failed clone blocks its card permanently.
Measured 2026-09-21 on chiap03: 634 of 714 workspaces were unusable (465
empty, 169 non-git). Card d621aeec was bound to one whose only content was an
empty nested clone with no remotes, no HEAD and a clean tree.

Deleting a workspace that held real work is unrecoverable, so all three
conditions are asserted here independently.
"""

import ast
import subprocess
from pathlib import Path

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _fn():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    body = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_failed_materialization"
    ]
    assert body, "_failed_materialization not found"
    namespace = {"subprocess": subprocess}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_failed_materialization"]


def _git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], capture_output=True, check=False)


def test_a_plain_directory_is_a_failed_materialization(tmp_path):
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")
    assert _fn()(tmp_path) is True


def test_an_empty_clone_with_no_remote_and_no_head_is_failed(tmp_path):
    """The exact d621aeec shape."""
    _git(tmp_path, "init", "-q")
    assert _fn()(tmp_path) is True


def test_a_repo_with_a_remote_is_never_discarded(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "remote", "add", "origin", "https://example.invalid/x.git")
    assert _fn()(tmp_path) is False


def test_a_repo_with_a_commit_is_never_discarded(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("work", encoding="utf-8")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-qm", "work")
    assert _fn()(tmp_path) is False


def test_a_repo_with_uncommitted_work_is_never_discarded(tmp_path):
    """Custody state is exactly what must survive."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "unsaved.txt").write_text("precious", encoding="utf-8")
    _git(tmp_path, "add", "unsaved.txt")
    assert _fn()(tmp_path) is False


def test_the_materialize_path_records_a_receipt_before_discarding():
    """Guards the wiring, and that the discard is auditable."""
    source = ROTATE.read_text(encoding="utf-8")
    assert "_failed_materialization(checkout, runner=runner)" in source
    assert "WORKSPACE_REMATERIALIZE" in source
    marker = "WORKSPACE_REMATERIALIZE"
    tail = source[source.index(marker) : source.index(marker) + 300]
    assert "shutil.rmtree" in tail, "the discard must follow its receipt"
