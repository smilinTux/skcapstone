from __future__ import annotations

import hashlib
import json
from pathlib import Path

import click
from click.testing import CliRunner

from skcapstone.cli.coord import register_coord_commands


def _main() -> click.Group:
    @click.group()
    def main() -> None:
        pass

    register_coord_commands(main)
    return main


def test_validate_store_emits_jsonl_and_fails_when_malformed(tmp_path: Path) -> None:
    event = tmp_path / "cards" / "abc12345" / "events" / "writer.jsonl"
    event.parent.mkdir(parents=True)
    raw = b'{"bad":}\n'
    event.write_bytes(raw)

    result = CliRunner().invoke(_main(), ["coord", "validate-store", "--home", str(tmp_path)])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "card": "abc12345",
        "file": "abc12345/events/writer.jsonl",
        "line": 1,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "reason": "Expecting value: line 1 column 8 (char 7)",
    }
    assert event.read_bytes() == raw


def test_validate_store_is_silent_success_for_clean_store(tmp_path: Path) -> None:
    event = tmp_path / "cards" / "abc12345" / "events" / "writer.jsonl"
    event.parent.mkdir(parents=True)
    event.write_text('{"ok":true}\n', encoding="utf-8")

    result = CliRunner().invoke(_main(), ["coord", "validate-store", "--home", str(tmp_path)])

    assert result.exit_code == 0
    assert result.output == ""
