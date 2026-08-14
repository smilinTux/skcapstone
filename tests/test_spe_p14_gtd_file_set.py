"""SPE P1.4: one GTD file-set constant, with archive inside the lookup universe.

Card ``3df69da1`` (sprint ``83482526``, epic ``373a33ca``). ``archive.json`` was
in the DEDUPE universe (``_seen_refs``) but NOT the LOOKUP universe
(``_find_item_across_lists`` iterated ``_GTD_LISTS`` only). An archived item
carrying a ``source_ref`` was therefore un-findable AND un-recapturable at the
same time: dedupe refused a fresh capture because the ref was already "in the
store", while every lookup verb reported it missing. On 2026-08-13 an item
survived that only because it happened to carry no ``source_ref``.

Three modules disagreed on the file set: ``gtd_tools`` (no archive in lookup),
``agent_run`` (archive included), ``skos.adapters.order`` (archive included).
They now all read one shared constant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")


def _write(name: str, items: list[dict]) -> None:
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir

    (_gtd_dir() / name).write_text(json.dumps(items), encoding="utf-8")


def test_store_files_include_archive():
    from skcapstone.mcp_tools.gtd_tools import GTD_STORE_FILES

    assert "archive.json" in GTD_STORE_FILES
    assert "inbox.json" in GTD_STORE_FILES


def test_agent_run_reads_the_shared_file_set():
    """agent_run kept its own copy of the layout; it must not any more."""
    from skcapstone import agent_run
    from skcapstone.mcp_tools.gtd_tools import GTD_STORE_FILES

    assert set(agent_run._GTD_LIST_FILES.values()) == set(GTD_STORE_FILES)


def test_archived_item_with_a_source_ref_is_findable():
    """The bug: deduped against, but invisible to every lookup."""
    from skcapstone.mcp_tools.gtd_tools import _find_item_across_lists, _seen_refs

    _write(
        "archive.json",
        [
            {
                "id": "arch01",
                "text": "done thing",
                "status": "done",
                "source": "email",
                "source_ref": "gmail:abc123",
            }
        ],
    )
    assert ("email", "gmail:abc123") in _seen_refs()  # already true: dedupe universe
    list_name, item, idx = _find_item_across_lists("arch01")
    assert list_name == "archive"
    assert item["id"] == "arch01"
    assert idx == 0


def test_live_lists_still_win_over_the_archive():
    """A live item shadows a same-id archived one; lookup order is lists first."""
    from skcapstone.mcp_tools.gtd_tools import _find_item_across_lists

    _write("inbox.json", [{"id": "dup01", "text": "live", "status": "inbox"}])
    _write("archive.json", [{"id": "dup01", "text": "archived", "status": "done"}])
    list_name, item, _ = _find_item_across_lists("dup01")
    assert list_name == "inbox"
    assert item["text"] == "live"


def test_archive_is_loadable_and_savable_by_list_name():
    """Lookup returning "archive" is useless unless the same name round-trips."""
    from skcapstone.mcp_tools.gtd_tools import _load_list, _save_list

    _save_list("archive", [{"id": "a1", "text": "x", "status": "done"}])
    assert [it["id"] for it in _load_list("archive")] == ["a1"]


def test_done_refuses_an_already_archived_item():
    """Now that done can SEE the archive, it must not re-archive a duplicate."""
    import asyncio

    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_done, _load_list

    _write("archive.json", [{"id": "arch02", "text": "already done", "status": "done"}])
    result = asyncio.run(_handle_gtd_done({"item_id": "arch02"}))
    payload = json.loads(result[0].text)
    assert payload.get("error")
    assert len(_load_list("archive")) == 1
