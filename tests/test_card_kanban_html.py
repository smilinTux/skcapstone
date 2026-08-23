"""Tests for the kanban HTML render (Phase 1, Task 5)."""

from __future__ import annotations

from skcapstone.card import KanbanBoard, render_html
from skcapstone.coordination import Board, Task


def test_render_html_contains_card_and_no_dashes(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    # Title carries an em dash and an en dash so _clean is actually exercised.
    board.create_task(
        Task(
            id="h1",
            title="Ren" + chr(0x2014) + "der" + chr(0x2013) + "me",
            created_by="opus",
            tags=["bug"],
        )
    )
    html_out = render_html(KanbanBoard(tmp_path), title="SKBoard")
    assert "<!doctype html" in html_out.lower()
    assert "Ren-der-me" in html_out  # _clean normalized both dashes to hyphens
    assert "backlog" in html_out.lower()
    # hard project rule: no em or en dashes in generated output.
    # Reference the characters by escape so this file itself stays dash-clean.
    assert chr(0x2014) not in html_out  # em dash
    assert chr(0x2013) not in html_out  # en dash


def test_render_html_escapes_titles(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="h2", title="<script>x</script>", created_by="opus"))
    html_out = render_html(KanbanBoard(tmp_path))
    assert "<script>x</script>" not in html_out
    assert "&lt;script&gt;" in html_out


def test_render_html_both_themes_present(tmp_path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="h3", title="theme card", created_by="o"))
    html_out = render_html(KanbanBoard(tmp_path))
    assert "prefers-color-scheme:dark" in html_out
    assert 'data-theme="dark"' in html_out
    assert 'data-theme="light"' in html_out
