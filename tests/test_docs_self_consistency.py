"""scripts/docs/check_self_consistency.py: a doc must not contradict itself."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "docs" / "check_self_consistency.py"
spec = importlib.util.spec_from_file_location("check_self_consistency", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def test_table_versus_prose_contradiction_is_caught(tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("The codex pool is max 4 on purpose.\n\n| backends |\n| codex 32, zai 10 |\n")
    assert any("codex gateway pool ceiling" in p for p in mod.scan(doc))


def test_consistent_document_passes(tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("The codex pool is max 32.\n\n| backends |\n| codex 32, zai 10 |\n")
    assert mod.scan(doc) == []


def test_history_in_running_prose_is_not_a_contradiction(tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("Chef raised codex 4 to 32 on 2026-08-25. The codex pool is max 32.\n")
    assert mod.scan(doc) == []


def test_unreadable_path_is_ignored(tmp_path):
    assert mod.scan(tmp_path / "missing.md") == []


def test_repo_docs_are_self_consistent(monkeypatch):
    monkeypatch.chdir(SCRIPT.parent.parent.parent)
    assert mod.main(["check_self_consistency.py"]) == 0
