"""Tests for the exit_gates producer and exit (task 11, card lifecycle gap).

``coord complete`` used to have no idea outstanding gates existed at all:
``await_gates`` was consumed by the dispatcher fold but produced nowhere.
This closes the gap from both ends: completing a gated card now records
``await_gates`` instead of ``complete``, and ``coord satisfy-gate`` is the
only way to clear a gate so a later completion can proceed.

The read path is the trap this whole task exists to avoid: ``exit_gates``
lives on ``core.json`` only, and the installed ``CardCore`` silently drops it
via pydantic's default extra="ignore". Every test here goes through the real
CLI and the real card_store, so a regression that switches to a CardCore/fold
read shows up as a failing test, not a silent no-op.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore

from skcapstone.cli import main
from skcapstone.coord_completion import (
    GatesPending,
    complete_coord_task,
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
    because CardCore has no field for it at all.
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
    """Pin the trap: CardCore drops exit_gates, our reader must not."""
    home = tmp_path / "home"
    _make_card(
        home,
        "cccc3333",
        "trap card",
        exit_gates=[{"gate": "g1", "owner": "seraph"}],
    )
    core_path = home / "cards" / "cccc3333" / "core.json"
    core = json.loads(core_path.read_text())

    # Confirm the trap is real: the installed CardCore silently drops the field.
    dumped = CardCore(**core).model_dump()
    assert "exit_gates" not in dumped

    # Our reader goes around CardCore entirely and still sees the gate.
    assert outstanding_gates(home, "cccc3333") == [{"gate": "g1", "owner": "seraph"}]


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

    assert result.exit_code == 0, result.output
    assert "independent-review" in result.output
    assert "seraph" in result.output


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
    assert first.exit_code == 0, first.output

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
