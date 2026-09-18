"""Regression checks for dispatcher import tree and worker workspace isolation."""

from pathlib import Path


ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"


def _source() -> str:
    return ROTATE.read_text(encoding="utf-8")


def test_dispatcher_fails_closed_when_import_comes_from_git_worktree() -> None:
    source = _source()

    assert "def _assert_dispatcher_import_is_release_managed" in source
    assert "branchable Git worktree" in source
    assert "_assert_dispatcher_import_is_release_managed()" in source
    assert source.index("_assert_dispatcher_import_is_release_managed()") < source.index(
        "run_production_cycle("
    )


def test_worker_workspace_rejects_dispatcher_import_tree_alias() -> None:
    source = _source()

    assert "def _reject_dispatcher_import_tree_workspace" in source
    workspace = source[source.index("def _worker_workspace"):source.index("def _source_workspace_spec")]
    assert "_reject_dispatcher_import_tree_workspace(workspace)" in workspace
    assert "worker workspace aliases dispatcher import tree" in source


def test_worker_launch_still_uses_a_distinct_working_directory() -> None:
    source = _source()

    assert '"--working-directory", workspace' in source
    assert "workspace=_materialize_worker_workspace(" in source
    assert source.index("workspace=_materialize_worker_workspace(") < source.index(
        "_worker_launch_command(unit,workspace,inner)"
    )
