"""Bounded, machine-readable ``coord status --format json`` (port of #320)."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coord_status_page import decode_cursor, encode_cursor, status_scope
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _board(tmp_path: Path, n: int = 12) -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    for i in range(n):
        tags = ["even"] if i % 2 == 0 else ["odd"]
        board.create_task(Task(id=f"aaaa{i:04d}", title=f"task {i}", tags=tags))


def _run(tmp_path: Path, *args: str):
    return CliRunner().invoke(_main(), ["coord", "status", "--home", str(tmp_path), *args])


def _ids(result) -> list[str]:
    return [c["id"] for c in json.loads(result.output)["cards"]]


def test_json_stdout_is_one_document(tmp_path):
    _board(tmp_path)
    result = _run(tmp_path, "--format", "json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["summary"]["total"] == 12
    assert len(payload["cards"]) == 12
    assert payload["has_more"] is False and payload["next_cursor"] is None
    assert "Coordination Board" not in result.output


def test_limit_bounds_and_cursor_pages_without_gap_or_overlap(tmp_path):
    _board(tmp_path)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        args = ["--format", "json", "--limit", "5"] + (["--cursor", cursor] if cursor else [])
        result = _run(tmp_path, *args)
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert len(payload["cards"]) <= 5
        seen += [c["id"] for c in payload["cards"]]
        pages += 1
        cursor = payload["next_cursor"]
        if not payload["has_more"]:
            assert cursor is None
            break
    assert pages == 3
    assert seen == [f"aaaa{i:04d}" for i in range(12)]


def test_exact_multiple_of_limit_reports_no_more(tmp_path):
    _board(tmp_path, 10)
    payload = json.loads(_run(tmp_path, "--format", "json", "--limit", "10").output)
    assert len(payload["cards"]) == 10 and payload["has_more"] is False


def test_tampered_cursor_fails_closed(tmp_path):
    _board(tmp_path)
    cursor = json.loads(_run(tmp_path, "--format", "json", "--limit", "5").output)["next_cursor"]
    tampered = cursor[:-4] + ("AAAA" if not cursor.endswith("AAAA") else "BBBB")
    for bad in (tampered, "not-a-cursor", "!!!!", "A" * 5000):
        result = _run(tmp_path, "--format", "json", "--limit", "5", "--cursor", bad)
        assert result.exit_code != 0
        assert "cursor" in result.output.lower()


def test_cursor_bound_to_limit_and_filters(tmp_path):
    _board(tmp_path)
    cursor = json.loads(_run(tmp_path, "--format", "json", "--limit", "5").output)["next_cursor"]
    # different limit
    assert _run(tmp_path, "--format", "json", "--limit", "4", "--cursor", cursor).exit_code != 0
    # different filter scope
    assert (
        _run(
            tmp_path, "--format", "json", "--limit", "5", "--tag", "even", "--cursor", cursor
        ).exit_code
        != 0
    )
    # a cursor minted for another limit by the encoder is refused as well
    other = encode_cursor("aaaa0004", 7, status_scope((), None, None))
    assert _run(tmp_path, "--format", "json", "--limit", "5", "--cursor", other).exit_code != 0


@pytest.mark.parametrize("limit", ["0", "-3"])
def test_non_positive_limit_rejected(tmp_path, limit):
    _board(tmp_path)
    result = _run(tmp_path, "--format", "json", "--limit", limit)
    assert result.exit_code != 0 and "limit" in result.output.lower()


def test_cursor_without_limit_and_text_mode_misuse_rejected(tmp_path):
    _board(tmp_path)
    cursor = json.loads(_run(tmp_path, "--format", "json", "--limit", "5").output)["next_cursor"]
    assert _run(tmp_path, "--format", "json", "--cursor", cursor).exit_code != 0
    assert _run(tmp_path, "--limit", "5").exit_code != 0


def test_filters_apply_before_paging(tmp_path):
    _board(tmp_path)
    result = _run(tmp_path, "--format", "json", "--tag", "odd", "--limit", "3")
    assert _ids(result) == ["aaaa0001", "aaaa0003", "aaaa0005"]
    assert json.loads(result.output)["has_more"] is True


def test_done_hidden_unless_requested(tmp_path):
    _board(tmp_path, 3)
    board = Board(tmp_path)
    board.claim_task("tester", "aaaa0000")
    board.complete_task("tester", "aaaa0000")
    assert "aaaa0000" not in _ids(_run(tmp_path, "--format", "json"))
    assert "aaaa0000" in _ids(_run(tmp_path, "--format", "json", "--include-done"))
    assert json.loads(_run(tmp_path, "--format", "json").output)["summary"]["done"] == 1


def test_malformed_cardstore_event_does_not_crash_json_status(tmp_path):
    _board(tmp_path, 2)
    bad = tmp_path / "cards" / "deadbeef" / "events"
    bad.mkdir(parents=True)
    (bad / "w.jsonl").write_text("{bad\n", encoding="utf-8")
    result = _run(tmp_path, "--format", "json")
    assert result.exit_code == 0, result.output
    assert "aaaa0000" in _ids(result)


def test_decode_cursor_rejects_non_string():
    for bad in (None, 5, b"x", ["a"]):
        with pytest.raises(ValueError):
            decode_cursor(bad, 5, "s")
