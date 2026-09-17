"""Tests for the exit_gates producer and exit (task 11, card lifecycle gap).

``coord complete`` used to have no idea outstanding gates existed at all:
``await_gates`` was consumed by the dispatcher fold but produced nowhere.
This closes the gap from both ends: completing a gated card now records
``await_gates`` instead of ``complete``, and ``coord satisfy-gate`` is the
only way to clear a gate so a later completion can proceed.

The read path is the trap this whole task exists to avoid. ``exit_gates`` is
written straight onto ``core.json``, and every reader here goes through the
real CLI and the real card_store rather than a model. That independence is the
point: skcoord versions before 0.1.77 had no ``exit_gates`` field on
``CardCore`` and, with pydantic defaulting to extra="ignore", dropped it
silently, so a fold-based read saw an empty list on every card and enforced
nothing while looking healthy. 0.1.77 added the field, but a node mid-rollout
or pinned older still behaves the old way, so the read path must not depend on
the model either way.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.cli import main
from skcapstone.coord_completion import (
    GatesPending,
    _read_core_exit_gates,
    complete_coord_task,
    move_coord_task,
    outstanding_gates,
    satisfy_gate,
)
from skcapstone.coordination import Board, Task


def _make_card(
    home: Path,
    task_id: str,
    title: str,
    agent: str = "reviewer",
    exit_gates: list[dict] | None = None,
) -> None:
    """Create a claimed, non-review card, optionally with exit_gates on core.json.

    exit_gates is never passed through CardCore: it is written straight onto
    core.json after creation, the same way the real backfill tooling does it,
    because the read path must not depend on the model carrying the field.
    """
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title))
    board.claim_task(agent, task_id)
    if exit_gates is not None:
        core_path = home / "cards" / task_id / "core.json"
        core = json.loads(core_path.read_text())
        core["exit_gates"] = exit_gates
        core_path.write_text(json.dumps(core))


def _events(home: Path, task_id: str) -> list[dict]:
    events_dir = home / "cards" / task_id / "events"
    out: list[dict] = []
    if not events_dir.exists():
        return out
    for log in events_dir.glob("*.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_card_without_exit_gates_completes_exactly_as_today(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "aaaa1111", "plain card, no gates")

    result = complete_coord_task(home, "reviewer", "aaaa1111")

    assert not isinstance(result, GatesPending)
    assert result.agent == "reviewer"
    assert "aaaa1111" in result.completed_tasks
    actions = [e.get("action") for e in _events(home, "aaaa1111")]
    assert "complete" in actions
    assert "await_gates" not in actions


def test_card_with_one_outstanding_gate_gets_await_gates_not_complete(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "bbbb2222",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    result = complete_coord_task(home, "reviewer", "bbbb2222")

    assert isinstance(result, GatesPending)
    assert result.outstanding == [{"gate": "independent-review", "owner": "seraph"}]
    actions = [e.get("action") for e in _events(home, "bbbb2222")]
    assert "complete" not in actions
    assert "await_gates" in actions


def test_exit_gates_are_read_from_core_json_not_the_cardcore_model(
    tmp_path: Path,
) -> None:
    """The reader must read core.json directly, never through CardCore.

    History, because this test used to assert the opposite and went red the
    moment it came true. Older installed skcoord versions lacked exit_gates on
    CardCore and, with pydantic defaulting to extra="ignore", silently dropped
    the field on load. A reader going through CardStore.fold or any CardCore
    path therefore saw an empty list on every card, passed every test against
    real data, and enforced nothing.

    skcoord 0.1.77 added the field, so asserting that CardCore drops it now
    fails. That assertion was never the property worth pinning: it encoded which
    skcoord happened to be installed. What matters is that this reader does not
    depend on the model carrying the field, so it stays correct on a node that
    is mid-rollout or pinned to an older skcoord. Do not reintroduce a
    CardCore/fold read here.
    """
    home = tmp_path / "home"
    _make_card(
        home,
        "cccc3333",
        "trap card",
        exit_gates=[{"gate": "g1", "owner": "seraph"}],
    )
    # The reader goes around CardCore entirely, whatever the model does.
    assert outstanding_gates(home, "cccc3333") == [{"gate": "g1", "owner": "seraph"}]

    # Pin the independence structurally, not by asserting what the installed
    # CardCore happens to do. A value assertion alone stopped catching the
    # regression the moment skcoord 0.1.77 added the field, because a
    # model-based reader started returning the right answer here while still
    # being wrong on any node running an older skcoord.
    tree = ast.parse(textwrap.dedent(inspect.getsource(_read_core_exit_gates)))
    fn = tree.body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.dump(node) for node in body)
    assert "core.json" in code
    assert "CardCore" not in code
    assert "fold" not in code


def test_satisfy_gate_rejects_a_name_not_in_exit_gates(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "dddd4444",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    with pytest.raises(ValueError, match="typo-gate"):
        satisfy_gate(home, "dddd4444", "typo-gate", "seraph")


def test_satisfy_gate_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "eeee5555",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    first = satisfy_gate(home, "eeee5555", "independent-review", "seraph")
    second = satisfy_gate(home, "eeee5555", "independent-review", "seraph")

    assert first is True
    assert second is False
    satisfied = [e for e in _events(home, "eeee5555") if e.get("action") == "gate_satisfied"]
    assert len(satisfied) == 1


def test_complete_records_complete_once_every_gate_is_satisfied(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "ffff6666",
        "gated card",
        exit_gates=[{"gate": "g1", "owner": "seraph"}],
    )

    pending = complete_coord_task(home, "reviewer", "ffff6666")
    assert isinstance(pending, GatesPending)

    satisfy_gate(home, "ffff6666", "g1", "seraph")
    result = complete_coord_task(home, "reviewer", "ffff6666")

    assert not isinstance(result, GatesPending)
    assert "ffff6666" in result.completed_tasks
    actions = [e.get("action") for e in _events(home, "ffff6666")]
    assert "complete" in actions


def test_cli_complete_reports_outstanding_gate_and_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "11112222",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    result = CliRunner().invoke(
        main,
        ["coord", "complete", "11112222", "--home", str(home), "--agent", "reviewer"],
    )

    assert result.exit_code == 3, result.output
    assert "independent-review" in result.output
    assert "seraph" in result.output


def test_cli_complete_gated_exit_code_is_distinct_from_success_and_error(
    tmp_path: Path,
) -> None:
    """A gated complete must not read as returncode 0 to a caller.

    Fleet's close_reviewed_parents() checks returncode == 0 to decide whether
    a card actually closed; a gated card exiting 0 was a false completion.
    Exit code 1 is already this command's ValueError path, so the gated
    outcome needs its own code rather than reusing either.
    """
    home = tmp_path / "home"
    _make_card(
        home,
        "55556666",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    result = CliRunner().invoke(
        main,
        ["coord", "complete", "55556666", "--home", str(home), "--agent", "reviewer"],
    )

    assert result.exit_code not in (0, 1)
    assert result.exit_code == 3


def test_cli_satisfy_gate_then_complete_round_trip(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "33334444",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )
    runner = CliRunner()

    first = runner.invoke(
        main,
        ["coord", "complete", "33334444", "--home", str(home), "--agent", "reviewer"],
    )
    assert first.exit_code == 3, first.output

    satisfy = runner.invoke(
        main,
        [
            "coord",
            "satisfy-gate",
            "33334444",
            "--home",
            str(home),
            "--gate",
            "independent-review",
            "--agent",
            "seraph",
        ],
    )
    assert satisfy.exit_code == 0, satisfy.output

    second = runner.invoke(
        main,
        ["coord", "complete", "33334444", "--home", str(home), "--agent", "reviewer"],
    )
    assert second.exit_code == 0, second.output
    assert "Completed" in second.output


def test_cli_satisfy_gate_rejects_unknown_name(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "55556666",
        "gated card",
        exit_gates=[{"gate": "g1", "owner": "seraph"}],
    )

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "satisfy-gate",
            "55556666",
            "--home",
            str(home),
            "--gate",
            "typo",
            "--agent",
            "seraph",
        ],
    )

    assert result.exit_code == 1


# --- move-to-done bypass (task 11) -----------------------------------------
#
# ``coord move <task_id> done`` reaches the board through
# ``skcoord.lifecycle.transition_task`` instead of ``complete_coord_task``,
# and never ran the gate check above, so a card with outstanding exit_gates
# could be moved straight to done with no gate ever satisfied. move_coord_task
# refuses the move instead of silently converting it into an await_gates
# card: a worker scripting a move to done wants to know immediately that the
# move did not happen, not discover later that it turned into something else.


def test_move_to_done_with_outstanding_gate_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "66667777",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    with pytest.raises(ValueError, match="independent-review"):
        move_coord_task(home, "reviewer", "66667777", "done")

    actions = [e.get("action") for e in _events(home, "66667777")]
    assert "move" not in actions


def test_move_to_done_without_exit_gates_is_unchanged(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "77778888", "plain card, no gates")

    move_coord_task(home, "reviewer", "77778888", "done")

    actions = [e.get("action") for e in _events(home, "77778888")]
    assert "move" in actions


def test_move_to_non_done_column_with_outstanding_gate_is_unchanged(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "88889999",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    move_coord_task(home, "reviewer", "88889999", "review")

    actions = [e.get("action") for e in _events(home, "88889999")]
    assert "move" in actions


def test_move_to_done_succeeds_after_all_gates_satisfied(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "9999aaaa",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    satisfy_gate(home, "9999aaaa", "independent-review", "seraph")
    move_coord_task(home, "reviewer", "9999aaaa", "done")

    actions = [e.get("action") for e in _events(home, "9999aaaa")]
    assert "move" in actions


def test_cli_move_to_done_reports_outstanding_gate_and_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(
        home,
        "aaaabbbb",
        "gated card",
        exit_gates=[{"gate": "independent-review", "owner": "seraph"}],
    )

    result = CliRunner().invoke(
        main,
        ["coord", "move", "aaaabbbb", "done", "--home", str(home), "--agent", "reviewer"],
    )

    assert result.exit_code != 0
    assert "independent-review" in result.output
    assert "seraph" in result.output
    assert "satisfy-gate" in result.output
