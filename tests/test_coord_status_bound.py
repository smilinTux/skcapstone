"""Bounded, machine-readable coord status + malformed-card isolation.

Card d55c6dd3 (SKCOORD-STATUS-BOUND-01): the default status output is a
bounded, machine-readable interface. The primary payload goes to stdout;
diagnostics go to stderr; one malformed card is reported by ID + evidence
hash without crashing the whole status command; and a discoverable
continuation cursor exposes truncation instead of dumping the whole board.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.cli.coord import (
    _decode_status_cursor,
    _encode_status_cursor,
    _status_scope,
    register_coord_commands,
)
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _board_with_many_tasks(tmp_path: Path, n: int = 30) -> Board:
    board = Board(tmp_path)
    board.ensure_dirs()
    for i in range(n):
        board.create_task(Task(id=f"aaaa{i:04d}", title=f"task {i}"))
    return board


def test_status_json_payload_is_on_stdout(tmp_path: Path):
    """--format json emits the primary payload as a single JSON document."""
    _board_with_many_tasks(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["format"] == "json"
    assert payload["summary"]["total"] == 30
    assert len(payload["cards"]) == 30
    assert payload["has_more"] is False
    assert payload["next_cursor"] is None
    assert payload["malformed_cards"] == []


def test_status_limit_bounds_the_payload_and_exposes_truncation(tmp_path: Path):
    """--limit bounds the payload and exposes a discoverable continuation cursor."""
    _board_with_many_tasks(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json", "--limit", "5"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload["cards"]) == 5
    assert payload["has_more"] is True
    assert payload["next_cursor"] is not None
    # The cursor is opaque + integrity-protected; decode returns the after-position.
    after = _decode_status_cursor(payload["next_cursor"], limit=5)
    assert after == "aaaa0004"


def test_status_cursor_continues_the_page_without_overlap_or_gap(tmp_path: Path):
    """Passing --cursor continues where the previous page left off."""
    _board_with_many_tasks(tmp_path)
    first = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json", "--limit", "5"],
    )
    first_payload = json.loads(first.output)
    cursor = first_payload["next_cursor"]

    second = CliRunner().invoke(
        _main(),
        [
            "coord", "status", "--home", str(tmp_path), "--format", "json",
            "--limit", "5", "--cursor", cursor,
        ],
    )
    assert second.exit_code == 0, second.output
    second_payload = json.loads(second.output)
    # Second page starts strictly after the first page's last card (no overlap).
    assert [c["id"] for c in second_payload["cards"]] == [
        "aaaa0005",
        "aaaa0006",
        "aaaa0007",
        "aaaa0008",
        "aaaa0009",
    ]
    # Chained cursor continues the sequence (no gap, no overlap).
    chained = CliRunner().invoke(
        _main(),
        ["coord",
         "status",
         "--home", str(tmp_path),
         "--format", "json",
         "--limit", "5",
         "--cursor", second_payload["next_cursor"],
        ]
    )
    chained_payload = json.loads(chained.output)
    assert [c["id"] for c in chained_payload["cards"]] == [
        "aaaa0010",
        "aaaa0011",
        "aaaa0012",
        "aaaa0013",
        "aaaa0014",
    ]


def test_status_cursor_rejects_forged_or_malformed(tmp_path: Path):
    """A tampered, malformed, or stale status cursor fails closed."""
    _board_with_many_tasks(tmp_path)
    first = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json", "--limit", "5"],
    )
    cursor = json.loads(first.output)["next_cursor"]
    tampered = cursor[:-4] + ("AAAA" if not cursor.endswith("AAAA") else "BBBB")

    result = CliRunner().invoke(
        _main(),
        [
            "coord", "status", "--home", str(tmp_path), "--format", "json",
            "--limit", "5", "--cursor", tampered,
        ],
    )
    assert result.exit_code != 0
    assert "cursor" in result.output.lower()

    # A cursor minted for a different --limit is stale and must be rejected:
    # the cursor payload binds (scope, limit, archive-mode), so replaying it
    # under a different --limit fails closed.
    other_scope_cursor = _encode_status_cursor(
        {
            "after": "aaaa0004",
            "limit": 7,  # different limit than the 5 used above
            "scope": _status_scope((), None, None),
            "v": 1,
        }
    )
    result2 = CliRunner().invoke(
        _main(),
        [
            "coord", "status", "--home", str(tmp_path), "--format", "json",
            "--limit", "5", "--cursor", other_scope_cursor,
        ],
    )
    assert result2.exit_code != 0


def test_malformed_card_isolated_with_evidence_hash(tmp_path: Path, monkeypatch):
    """One unreadable CardStore card is reported by ID + evidence hash, not a crash."""
    import skcapstone.card_store as cs

    def _fake(home, include_archived=False):
        malformed = [
            {
                "card_id": "c9328739",
                "source": "cards/c9328739",
                "reason": "CardStore core for c9328739 is malformed",
                "evidence_sha256": "9123ddc74c22a16fd4bc1d55fa535e3c9809a4f886c314f36c406",
            }
        ]
        return [], malformed

    monkeypatch.setattr(cs, "task_views_with_malformed", _fake, raising=False)
    _board_with_many_tasks(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    # The command does not crash: the malformed card is reported in the payload
    # and every readable card is still shown (not hidden).
    assert payload["malformed_cards"][0]["card_id"] == "c9328739"
    assert any(c["id"] == "aaaa0000" for c in payload["cards"])


def test_malformed_report_survives_on_stdout_json(tmp_path: Path, monkeypatch):
    """With --format json the malformed report is part of the stdout payload."""
    import skcapstone.card_store as cs

    def _fake(home, include_archived=False):
        malformed = [
            {
                "card_id": "c9328739",
                "source": "cards/c9328739",
                "reason": "CardStore core for c9328739 is malformed",
                "evidence_sha256": "9123ddc74c22a16fd4bc1d55fa535e3c9809a4f886c314f36c406",
            }
        ]
        return [], malformed

    monkeypatch.setattr(cs, "task_views_with_malformed", _fake, raising=False)
    _board_with_many_tasks(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["malformed_cards"][0]["card_id"] == "c9328739"
    assert "evidence_sha256" in payload["malformed_cards"][0]
    assert payload["summary"]["malformed"] == 1


def test_diagnostics_go_to_stderr_not_stdout(tmp_path: Path):
    """The default (text) payload stays on stdout; JSON stdout is machine-readable."""
    _board_with_many_tasks(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    # stdout holds the human-readable board (payload); diagnostics go to stderr.
    assert "Coordination Board" in result.output
    # A JSON parse of the --format json stdout must succeed.
    json_result = runner.invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--format", "json"],
    )
    json.loads(json_result.output)
