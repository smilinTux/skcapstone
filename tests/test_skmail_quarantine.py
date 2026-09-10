"""Self-heal quarantine tests for the canonical SKMail writer (card 4a3d1119)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts" / "fleet" / "skmail_writer.py"
SPEC = importlib.util.spec_from_file_location("skmail_writer_quarantine", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


FOREIGN = (
    '{"ts":"2026-09-04T21:39:45.675972+00:00","from":"pi-chiap08","to":"jarvis",'
    '"priority":"normal","re":"poison","body":"foreign writer record","host":"chiap08"}\n'
)


def _poisoned_box(tmp_path: Path) -> Path:
    MODULE.append(tmp_path, "jarvis", "lumina", "fyi", "legit", "body", "chiap08")
    box = tmp_path / "jarvis@chiap08.jsonl"
    box.write_text(box.read_text(encoding="utf-8") + FOREIGN)
    return box


def test_quarantine_moves_foreign_record_losslessly(tmp_path: Path) -> None:
    box = _poisoned_box(tmp_path)
    pre = box.read_text(encoding="utf-8")

    moved = MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08")

    assert moved == 1
    dest = tmp_path / "pi-chiap08@chiap08.jsonl"
    assert dest.read_text(encoding="utf-8") == FOREIGN  # byte-identical
    assert FOREIGN not in box.read_text(encoding="utf-8")
    legit = json.loads(box.read_text(encoding="utf-8"))
    assert legit["from"] == "jarvis"  # legitimate record preserved
    assert sorted(box.read_text(encoding="utf-8").splitlines()) == sorted(pre.splitlines()[:1])


def test_send_unblocked_after_quarantine(tmp_path: Path) -> None:
    _poisoned_box(tmp_path)
    # The poison blocks the canonical append...
    try:
        MODULE.append(tmp_path, "jarvis", "lumina", "normal", "x", "y", "chiap08")
    except ValueError:
        pass
    else:
        raise AssertionError("poisoned mailbox accepted an append")
    # ...quarantine heals it...
    assert MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08") == 1
    # ...and the append now succeeds.
    MODULE.append(tmp_path, "jarvis", "lumina", "normal", "x", "y", "chiap08")
    lines = (tmp_path / "jarvis@chiap08.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["from"] == "jarvis" for line in lines)


def test_quarantine_clean_mailbox_is_noop(tmp_path: Path) -> None:
    MODULE.append(tmp_path, "jarvis", "lumina", "fyi", "a", "b", "chiap08")
    before = (tmp_path / "jarvis@chiap08.jsonl").read_text(encoding="utf-8")
    assert MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08") == 0
    assert (tmp_path / "jarvis@chiap08.jsonl").read_text(encoding="utf-8") == before


def test_quarantine_never_deletes_unparseable_lines(tmp_path: Path) -> None:
    MODULE.append(tmp_path, "jarvis", "lumina", "fyi", "legit", "body", "chiap08")
    box = tmp_path / "jarvis@chiap08.jsonl"
    box.write_text(box.read_text(encoding="utf-8") + FOREIGN + "not json at all\n")

    moved = MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08")

    assert moved == 1
    content = box.read_text(encoding="utf-8")
    assert "not json at all" in content  # untouched, still visible, still failing
    assert FOREIGN not in content


def test_quarantine_groups_foreign_by_destination(tmp_path: Path) -> None:
    MODULE.append(tmp_path, "jarvis", "lumina", "fyi", "legit", "body", "chiap08")
    second = (
        '{"ts":"2026-09-04T21:40:00Z","from":"mero","to":"jarvis",'
        '"priority":"fyi","re":"other","body":"other host","host":"chiap01"}\n'
    )
    box = tmp_path / "jarvis@chiap08.jsonl"
    box.write_text(box.read_text(encoding="utf-8") + FOREIGN + second)

    moved = MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08")

    assert moved == 2
    assert (tmp_path / "pi-chiap08@chiap08.jsonl").read_text(encoding="utf-8") == FOREIGN
    assert (tmp_path / "mero@chiap01.jsonl").read_text(encoding="utf-8") == second


def test_quarantine_refuses_contaminated_destination(tmp_path: Path) -> None:
    _poisoned_box(tmp_path)
    dest = tmp_path / "pi-chiap08@chiap08.jsonl"
    dest.write_text(
        '{"ts":"z","from":"someone-else","to":"x","priority":"fyi",'
        '"re":"y","body":"z","host":"chiap08"}\n'
    )
    try:
        MODULE.quarantine_foreign(tmp_path, "jarvis", "chiap08")
    except ValueError:
        return
    raise AssertionError("contaminated destination accepted")
