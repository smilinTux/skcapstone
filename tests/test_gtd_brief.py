"""`skcapstone gtd status --brief` — one-line summary for the SessionStart hook."""
import json
from pathlib import Path

import click
from click.testing import CliRunner

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.cli.gtd import register_gtd_commands


def _app(tmp_path: Path, monkeypatch) -> click.Group:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    gtd_dir = tmp_path / "coordination" / "gtd"
    gtd_dir.mkdir(parents=True)
    (gtd_dir / "inbox.json").write_text(
        json.dumps([{"id": "a", "text": "one"}, {"id": "b", "text": "two"}]),
        encoding="utf-8",
    )
    (gtd_dir / "next-actions.json").write_text(
        json.dumps([{"id": "c", "text": "do"}]), encoding="utf-8"
    )

    @click.group()
    def main() -> None:
        pass

    register_gtd_commands(main)
    return main


def test_brief_is_single_line_with_counts(tmp_path: Path, monkeypatch):
    res = CliRunner().invoke(_app(tmp_path, monkeypatch), ["gtd", "status", "--brief"])
    assert res.exit_code == 0
    out = res.output.strip()
    assert out.count("\n") == 0  # exactly one line
    assert out.startswith("GTD:")
    assert "2 inbox" in out and "1 next" in out


def test_brief_differs_from_full(tmp_path: Path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    brief = CliRunner().invoke(app, ["gtd", "status", "--brief"]).output
    full = CliRunner().invoke(app, ["gtd", "status"]).output
    assert len(brief.strip().splitlines()) == 1
    assert len(full.strip().splitlines()) > 1  # full is the rich multi-line panel
