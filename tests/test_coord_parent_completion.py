"""Parent cards remain open until every labeled child is terminal."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore
from skcoord.coordination import Board, Task

from skcapstone.cli.coord import register_coord_commands


def _main() -> click.Group:
    """Build the coordination CLI used by the focused tests."""

    @click.group()
    def main() -> None:
        pass

    register_coord_commands(main)
    return main


def _parent_with_child(home: Path, child_id: str = "bbbbbbb1") -> CardStore:
    """Create a parent and one labeled child in the real CardStore."""
    store = CardStore(home)
    store.create(CardCore(id="aaaaaaaa", title="Parent", created_by="test"))
    store.create(
        CardCore(
            id=child_id,
            title="Child",
            created_by="test",
            initial_labels=["parent-aaaaaaaa"],
        )
    )
    return store


@pytest.mark.parametrize("command", [["complete"], ["move", "done"]])
def test_cli_refuses_parent_completion_and_lists_exact_children(
    tmp_path: Path, command: list[str]
) -> None:
    """Both terminal CLI routes report every nonterminal child."""
    store = _parent_with_child(tmp_path, "bbbbbbb2")
    store.create(
        CardCore(
            id="bbbbbbb1",
            title="Child",
            created_by="test",
            initial_labels=["PARENT-aaaaaaaa"],
        )
    )

    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            *command[:1],
            "aaaaaaaa",
            *command[1:],
            "--home",
            str(tmp_path),
            "--agent",
            "test",
        ],
    )

    assert result.exit_code != 0
    assert "bbbbbbb1, bbbbbbb2" in result.output
    assert store.fold("aaaaaaaa").status.value != "done"


def test_parent_completes_after_all_children_are_terminal(tmp_path: Path) -> None:
    """Terminal child folds permit the normal append-only completion path."""
    store = _parent_with_child(tmp_path)
    store.append_event("bbbbbbb1", "complete", "test")

    result = CliRunner().invoke(
        _main(),
        ["coord", "complete", "aaaaaaaa", "--home", str(tmp_path), "--agent", "test"],
    )

    assert result.exit_code == 0, result.output
    assert store.fold("aaaaaaaa").status.value == "done"
    assert any(event["action"] == "complete" for event in store._read_events("aaaaaaaa"))


def test_concurrent_child_events_fold_before_parent_validation(tmp_path: Path) -> None:
    """Concurrent child terminal events leave a deterministic eligible fold."""
    store = _parent_with_child(tmp_path, "bbbbbbb1")
    for child_id in ("bbbbbbb2", "bbbbbbb3"):
        store.create(
            CardCore(
                id=child_id,
                title="Child",
                created_by="test",
                initial_labels=["parent-aaaaaaaa"],
            )
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        list(
            executor.map(
                lambda child_id: CardStore(tmp_path).append_event(child_id, "complete", "test"),
                ("bbbbbbb1", "bbbbbbb2", "bbbbbbb3"),
            )
        )

    from skcapstone.coord_completion import validate_parent_completion

    validate_parent_completion("aaaaaaaa", tmp_path)


@pytest.mark.parametrize("mutation", ["create", "relabel"])
def test_parent_completion_excludes_concurrent_child_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    """A competing child mutation cannot cross the terminal validation boundary."""
    store = _parent_with_child(tmp_path)
    store.append_event("bbbbbbb1", "complete", "test")
    board = Board(tmp_path)
    if mutation == "relabel":
        board.create_task(Task(id="cccccccc", title="Child"))

    validated = Event()
    release_validation = Event()
    mutation_done = Event()
    errors: list[BaseException] = []

    from skcapstone import coord_completion

    original = coord_completion.validate_parent_completion

    def synchronized_validation(task_id: str, home: Path) -> None:
        original(task_id, home)
        validated.set()
        assert release_validation.wait(timeout=5)

    monkeypatch.setattr(coord_completion, "validate_parent_completion", synchronized_validation)

    def complete_parent() -> None:
        try:
            coord_completion.complete_coord_task(tmp_path, "test", "aaaaaaaa")
        except BaseException as exc:
            errors.append(exc)

    def mutate_child() -> None:
        try:
            if mutation == "create":
                board.create_task(Task(id="cccccccc", title="Child", tags=["parent-aaaaaaaa"]))
            else:
                board.update_task("cccccccc", add_tags=["parent-aaaaaaaa"])
        except BaseException as exc:
            errors.append(exc)
        finally:
            mutation_done.set()

    completion_thread = Thread(target=complete_parent)
    completion_thread.start()
    assert validated.wait(timeout=5)
    mutation_thread = Thread(target=mutate_child)
    mutation_thread.start()
    assert not mutation_done.wait(timeout=0.2)
    release_validation.set()
    completion_thread.join(timeout=5)
    mutation_thread.join(timeout=5)

    assert errors == []
    assert store.fold("aaaaaaaa").status.value == "done"
    assert store.fold("cccccccc").status.value == "backlog"
