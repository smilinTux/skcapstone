"""Regression coverage for the PR 424 false-eligibility case (card 49573f1d).

Card 00bee285 traced the false-eligibility path to the PR 424 adapter
``src/skcapstone/scheduler_decision.py::card_scheduler_facts`` (commit
5fcbf378, successor 6cb9d559): it reconstructs scheduler facts from labels,
lifecycle, owner, dependencies, and verdict evidence, but it never derives
the scheduler-only gates the canonical runtime policy applies from folded
labels. A card labelled ``foreign-project`` must be refused by the
scheduling policy, yet the adapter leaves ``foreign_project=False`` and the
canonical decision surface reports ``eligible=true / ready``.

This module asserts ONLY against the canonical scheduling-policy decision
surface (the ``coord gates`` CLI JSON), never against a private eligibility
helper. The tests are expected to FAIL against the adapter at 6cb9d559 and
to pass once card 9b6c7220 reworks the adapter onto the canonical gates.
"""

import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _run(home: Path, card_id: str):
    return CliRunner().invoke(_main(), ["coord", "gates", card_id, "--home", str(home)])


def _card(home: Path, card_id: str, **kwargs) -> None:
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=card_id, title=kwargs.pop("title", card_id), **kwargs))


def _event(home: Path, card_id: str, value: str) -> None:
    path = home / "coordination" / "card_events" / "test.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "card_id": card_id,
        "action": "link",
        "link_key": "verdict",
        "link_value": value,
        "ts": "2026-01-01T00:00:00Z",
        "writer": "test",
    }
    with path.open("a", encoding="utf-8") as handle:
        line = json.dumps(event, sort_keys=True)
        json.loads(line)
        handle.write(line + "\n")


def _payload(result) -> dict:
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_gates_refuses_foreign_project_label(tmp_path: Path) -> None:
    """A folded ``foreign-project`` card must not report eligible.

    The canonical runtime policy folds the ``foreign-project`` label into a
    hard refusal, so the canonical decision surface must report
    ``eligible=false`` with ``primary_reason="foreign_project"``. The card
    here is otherwise perfectly dispatchable: it is a plain task in backlog,
    unowned, with a PASS verdict recorded in its separate evidence stream,
    so the only possible refusal reason is the foreign-project gate itself.
    """
    _card(
        tmp_path,
        "fee00001",
        title="Foreign project card must never dispatch",
        tags=["foreign-project"],
    )
    _event(tmp_path, "fee00001", "PASS")

    # Precondition: the fold really carries the label, so a failure below is
    # the adapter ignoring the scheduler-only gate, not a fixture problem.
    from skcapstone.card_store import CardStore

    folded = CardStore(tmp_path).fold("fee00001")
    assert folded is not None
    assert "foreign-project" in folded.labels

    payload = _payload(_run(tmp_path, "fee00001"))

    assert payload["card_id"] == "fee00001"
    assert payload["eligible"] is False
    assert payload["primary_reason"] == "foreign_project"
    assert "foreign_project" in payload["blocking_reasons"]


def test_gates_still_admits_card_without_foreign_project_label(tmp_path: Path) -> None:
    """Control: the foreign-project gate must fire on the label, not always."""
    _card(tmp_path, "acc00001", title="Ordinary dispatchable card")
    _event(tmp_path, "acc00001", "PASS")

    payload = _payload(_run(tmp_path, "acc00001"))

    assert payload["card_id"] == "acc00001"
    assert payload["eligible"] is True
    assert payload["primary_reason"] == "ready"
    assert payload["blocking_reasons"] == []
