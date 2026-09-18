"""``coord reopen`` is the missing operator escape hatch for a parked card.

The dispatcher's BLOCKED backoff already honours a ``reopen`` event newer than
the verdict, and until this command existed nothing but the legacy reconciler
could emit one, so a card parked on a blocker that had since finished could
only be freed by hand-editing the store.

Two things are load-bearing and easy to get wrong:

- the event must land in the CARD's own event stream, not the coordination
  overlay. The dispatcher reads the card stream; an overlay row is invisible
  to it and the card would stay parked while the log claimed success.
- a reason is mandatory. Without one the command is a reset button and the
  ledger cannot say why anything came back.
"""

from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path: Path, task_id: str) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title="parked", description="blocked on a finished card"))
    CardStore(tmp_path).create(CardCore(id=task_id, title="parked", description="body"))


def _reopen(tmp_path: Path, task_id: str, *extra: str):
    return CliRunner().invoke(
        _main(),
        [
            "coord",
            "reopen",
            task_id,
            "--home",
            str(tmp_path),
            "--agent",
            "fleet-blocker-referent-sweep",
            "--reason",
            "blocker-referent-settled",
            *extra,
        ],
    )


def _reopen_events(tmp_path: Path, task_id: str) -> list[dict]:
    return [
        event
        for event in CardStore(tmp_path)._read_events(task_id)
        if event.get("action") == "reopen"
    ]


def test_reopen_writes_one_attributed_event_to_the_card_stream(tmp_path: Path):
    _seed(tmp_path, "aaa11111")
    result = _reopen(tmp_path, "aaa11111", "--referent", "card:bbbbbbbb")
    assert result.exit_code == 0, result.output
    events = _reopen_events(tmp_path, "aaa11111")
    assert len(events) == 1
    assert events[0]["writer"].startswith("fleet-blocker-referent-sweep")
    assert events[0]["reason"] == "blocker-referent-settled"
    assert events[0]["referents"] == ["card:bbbbbbbb"]
    assert events[0]["ts"]


def test_reopen_leaves_the_column_alone_unless_asked(tmp_path: Path):
    """These cards are open, not archived. A reopen that silently moved them
    would rewrite lifecycle state the sweep has no business deciding."""
    _seed(tmp_path, "aaa22222")
    before = CardStore(tmp_path).fold("aaa22222").status
    assert _reopen(tmp_path, "aaa22222").exit_code == 0
    assert CardStore(tmp_path).fold("aaa22222").status == before


def test_a_repeated_transition_id_never_appends_a_second_reopen(tmp_path: Path):
    _seed(tmp_path, "aaa33333")
    token = "blocker-settled:0123456789abcdef"
    assert _reopen(tmp_path, "aaa33333", "--transition-id", token).exit_code == 0
    assert _reopen(tmp_path, "aaa33333", "--transition-id", token).exit_code == 0
    assert len(_reopen_events(tmp_path, "aaa33333")) == 1


def test_a_different_generation_token_does_append(tmp_path: Path):
    _seed(tmp_path, "aaa44444")
    assert _reopen(tmp_path, "aaa44444", "--transition-id", "blocker-settled:aaaa").exit_code == 0
    assert _reopen(tmp_path, "aaa44444", "--transition-id", "blocker-settled:bbbb").exit_code == 0
    assert len(_reopen_events(tmp_path, "aaa44444")) == 2


def test_an_empty_reason_is_refused(tmp_path: Path):
    _seed(tmp_path, "aaa55555")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "reopen",
            "aaa55555",
            "--home",
            str(tmp_path),
            "--agent",
            "lumina",
            "--reason",
            "   ",
        ],
    )
    assert result.exit_code != 0
    assert _reopen_events(tmp_path, "aaa55555") == []


def test_a_voided_card_is_never_reopened(tmp_path: Path):
    """Void is a terminal decision. CardStore refuses, and so must this."""
    _seed(tmp_path, "aaa66666")
    CardStore(tmp_path).append_event("aaa66666", "void", "chef", reason="superseded")
    result = _reopen(tmp_path, "aaa66666")
    assert result.exit_code != 0
    assert _reopen_events(tmp_path, "aaa66666") == []


def test_an_unknown_card_is_refused_rather_than_created(tmp_path: Path):
    Board(tmp_path).ensure_dirs()
    assert _reopen(tmp_path, "aaa77777").exit_code != 0
