"""SPE P1 sprint gate: no GTD write path bypasses the store lock.

Sprint ``83482526`` (epic ``373a33ca``). Card 4082d990 enumerated two unlocked
writers, the skcapstone CLI capture and skos ``mail.py``. Auditing the sprint's
acceptance criterion turned up a THIRD one it never named: the dreaming engine
appended its output to ``someday-maybe.json`` with a bare ``write_text``, so it
was neither serialized nor atomic. That is the whole reason the criterion is
phrased as "no path", not "these two paths".

The last test here is the enumeration guard: it fails on any NEW bare write to a
store file. Enumerating writers is what the authorization-standard incident
proved you cannot do by soaking.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")


def _dream_result():
    from skcapstone.dreaming import DreamResult

    return DreamResult(
        dreamed_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        insights=["i1"],
        connections=["c1"],
        questions=["q1"],
    )


def test_dreaming_capture_serializes_on_the_store_lock(tmp_path: Path):
    """Fail-before: a bare write_text sailed straight past a held lock."""
    from skcapstone.dreaming import DreamingEngine
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _store_lock

    _gtd_dir()
    order: list[str] = []
    holding = threading.Event()

    def hold_the_lock():
        with _store_lock():
            holding.set()
            time.sleep(0.25)
            order.append("holder-release")

    t_hold = threading.Thread(target=hold_the_lock)
    t_hold.start()
    assert holding.wait(2), "lock holder never started"

    def dream_write():
        DreamingEngine(home=tmp_path)._capture_to_gtd_someday(_dream_result())
        order.append("dream-done")

    t_dream = threading.Thread(target=dream_write)
    t_dream.start()
    t_hold.join(3)
    t_dream.join(3)

    assert order == ["holder-release", "dream-done"], f"dreaming bypassed the lock: {order}"


def test_dreaming_capture_is_atomic(tmp_path: Path, monkeypatch):
    """A crash at the rename must leave the previous list intact."""
    import os as _os

    from skcapstone.dreaming import DreamingEngine
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _load_list, _save_list

    _gtd_dir()
    _save_list("someday-maybe", [{"id": "pre01", "text": "was here first"}])

    def _boom(*a, **kw):
        raise OSError("crash at the rename")

    monkeypatch.setattr(_os, "replace", _boom)
    DreamingEngine(home=tmp_path)._capture_to_gtd_someday(_dream_result())

    assert [it["id"] for it in _load_list("someday-maybe")] == ["pre01"]
    assert list((_gtd_dir()).glob(".someday-maybe.json.*.tmp")) == []


def test_dreaming_capture_still_lands_its_items(tmp_path: Path):
    from skcapstone.dreaming import DreamingEngine
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _load_list

    _gtd_dir()
    DreamingEngine(home=tmp_path)._capture_to_gtd_someday(_dream_result())

    someday = _load_list("someday-maybe")
    assert len(someday) == 3
    assert all(it["status"] == "someday" and it["source"] == "dreaming-engine" for it in someday)
    assert _load_list("inbox") == []  # the actionable inbox stays clean


def test_atomic_write_lands_in_the_directory_it_was_given(tmp_path: Path):
    """_atomic_write_json delegates to the skos sink, which resolves paths by
    NAME under its own store dir. A target outside the store must not be
    silently redirected into it."""
    from skcapstone.mcp_tools.gtd_tools import _atomic_write_json, _gtd_dir

    _gtd_dir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "inbox.json"
    _atomic_write_json(target, [{"id": "outside"}])

    assert json.loads(target.read_text(encoding="utf-8")) == [{"id": "outside"}]
    assert json.loads((_gtd_dir() / "inbox.json").read_text(encoding="utf-8")) == []


def test_no_module_writes_a_store_file_behind_the_lock():
    """Enumeration guard: a new bare write to a GTD store file fails here.

    Deliberately a source scan rather than a runtime probe. An unenumerated
    write path is invisible to a soak by definition; it only shows up when you
    go looking for it, which is how this sprint found the dreaming engine.
    """
    import re

    import skcapstone
    from skcapstone.mcp_tools.gtd_tools import GTD_STORE_FILES

    names = "|".join(re.escape(f) for f in GTD_STORE_FILES)
    # A variable bound to a store-file path, e.g.  x = base / "inbox.json"
    bound_to_store = re.compile(rf'(\w+)\s*=\s*[^\n]*"(?:{names})"')

    src = Path(skcapstone.__file__).parent
    offenders: list[str] = []
    for py in sorted(src.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        suspects = set(bound_to_store.findall(text))
        if not suspects:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(f"{var}.write_text(" in line for var in suspects):
                offenders.append(f"{py.relative_to(src)}:{lineno}: {line.strip()}")

    assert offenders == [], "GTD store files must be written through _save_list:\n" + "\n".join(
        offenders
    )
