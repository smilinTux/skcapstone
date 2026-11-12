"""skgit is canonical: a GitHub PR cannot be linked to a skgit-repository card."""

from __future__ import annotations

from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.pr_binding import check_link_write, pr_forge_conflict

SKGIT = "https://skgit.skstack01.douno.it/smilinTux/sklegal.git"
GH_PR = "https://github.com/smilinTux/sklegal/pull/133"
SKGIT_PR = "https://skgit.skstack01.douno.it/smilinTux/sklegal/pulls/9"


def test_conflict_only_for_github_pr_on_skgit_repository():
    assert pr_forge_conflict(SKGIT, GH_PR)
    assert pr_forge_conflict(SKGIT, SKGIT_PR) is None
    assert pr_forge_conflict("https://github.com/smilinTux/sklegal", GH_PR) is None
    assert pr_forge_conflict(None, GH_PR) is None
    assert pr_forge_conflict(SKGIT, "12") is None


def test_check_link_write_covers_repository_and_meta():
    card = SimpleNamespace(links={"repository": SKGIT}, meta={})
    with pytest.raises(ValueError, match="card_pr_repository_mismatch"):
        check_link_write(card, "pr", GH_PR)
    check_link_write(card, "pr", SKGIT_PR)
    check_link_write(card, "commit", GH_PR)
    check_link_write(None, "pr", GH_PR)
    existing = SimpleNamespace(links={"pr": GH_PR}, meta={})
    with pytest.raises(ValueError):
        check_link_write(existing, "repository", SKGIT)
    github_card = SimpleNamespace(links={"repository": "https://github.com/a/b"}, meta={})
    check_link_write(github_card, "pr", GH_PR)


def _main():
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _link(home, key, value):
    return CliRunner().invoke(
        _main(),
        ["coord", "link", "deadbeef", key, value, "--agent", "lumina-nor", "--home", str(home)],
    )


def test_coord_link_refuses_github_pr_then_accepts_skgit_pr(tmp_path, monkeypatch):
    monkeypatch.setattr("skcapstone.jarvis_emergency.authorize_coord_mutation", lambda *a: None)
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="deadbeef", title="Card", description="body"))
    CardStore(tmp_path).create(CardCore(id="deadbeef", title="Card", description="body"))
    assert _link(tmp_path, "repository", SKGIT).exit_code == 0
    refused = _link(tmp_path, "pr", GH_PR)
    assert refused.exit_code != 0
    assert "skgit" in refused.output
    assert "pr" not in CardStore(tmp_path).fold("deadbeef").links
    assert _link(tmp_path, "pr", SKGIT_PR).exit_code == 0
    assert CardStore(tmp_path).fold("deadbeef").links["pr"] == SKGIT_PR
