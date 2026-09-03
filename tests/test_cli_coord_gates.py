"""Focused coverage for the read-only coord gates diagnostic."""

import json
from pathlib import Path

import click
from click.testing import CliRunner
from skcoord.card import Column
from skcoord.lifecycle import transition_task

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


def test_gates_reports_identity_revision_and_eligible_without_mutation(tmp_path: Path) -> None:
    _card(tmp_path, "aaa00001")
    before = sorted((p, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())

    payload = _payload(_run(tmp_path, "aaa00001"))

    assert payload["card_id"] == "aaa00001"
    assert len(payload["folded_revision"]) == 64
    assert payload["eligible"] is True
    assert payload["blocking_reasons"] == []
    assert before == sorted((p, p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())


def test_gates_reports_dependency_block(tmp_path: Path) -> None:
    _card(tmp_path, "dea00001")
    _card(tmp_path, "cad00001", dependencies=["dea00001"])
    payload = _payload(_run(tmp_path, "cad00001"))
    assert payload["blocking_reasons"] == ["dependency"]


def test_gates_reports_human_gate_and_dependency_as_all_reasons(tmp_path: Path) -> None:
    _card(tmp_path, "dea00001")
    _card(tmp_path, "cad00001", tags=["human-gate"], dependencies=["dea00001"])
    payload = _payload(_run(tmp_path, "cad00001"))
    assert payload["primary_reason"] == "human_gate"
    assert payload["blocking_reasons"] == ["human_gate", "dependency"]


def test_gates_reports_review_required(tmp_path: Path) -> None:
    _card(tmp_path, "cad00001")
    transition_task(tmp_path, task_id="cad00001", column=Column.REVIEW, actor="test")
    assert _payload(_run(tmp_path, "cad00001"))["blocking_reasons"] == ["awaiting_review"]


def test_gates_reports_backoff_from_explicit_evidence(tmp_path: Path) -> None:
    _card(tmp_path, "cad00001")
    _event(tmp_path, "cad00001", "BLOCKED")
    assert _payload(_run(tmp_path, "cad00001"))["blocking_reasons"] == ["backoff"]


def test_gates_reports_not_claimable(tmp_path: Path) -> None:
    _card(tmp_path, "cad00001", tags=["not-claimable"])
    assert _payload(_run(tmp_path, "cad00001"))["blocking_reasons"] == ["not_claimable"]


def test_gates_rejects_unknown_card(tmp_path: Path) -> None:
    result = _run(tmp_path, "abc00001")
    assert result.exit_code != 0
    assert "unknown card" in result.output


def test_gates_fails_closed_on_malformed_evidence(tmp_path: Path) -> None:
    _card(tmp_path, "cad00001")
    path = tmp_path / "coordination" / "card_events" / "broken.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json}\n", encoding="utf-8")
    result = _run(tmp_path, "cad00001")
    assert result.exit_code != 0
    assert "malformed card or evidence" in result.output
