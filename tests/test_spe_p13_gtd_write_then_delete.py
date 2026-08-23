"""SPE P1.3: GTD transfers write the destination first, delete the source second.

Card ``4562954e`` (sprint ``83482526``, epic ``373a33ca``). ``gtd done`` saved
the source list (item already removed) and only then saved the archive. A crash
in that window destroyed the item outright: gone from the list, never in the
archive. ``skos.gtd_ingest._upsert_locked`` deliberately does the opposite, so
a crash there duplicates instead of losing. This converges on the safe order.

Duplication is recoverable, loss is not. That is the whole trade.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")


class _WriteWindowCrashError(Exception):
    """Stands in for power loss / SIGKILL between the two saves."""


def _seed(list_name: str, items: list[dict]) -> None:
    from skcapstone.mcp_tools import gtd_tools

    gtd_tools._gtd_dir()
    gtd_tools._save_list(list_name, items)


def _crash_on_saving(monkeypatch, doomed_list: str) -> None:
    """Make exactly the source-list save explode, nothing else."""
    from skcapstone.mcp_tools import gtd_tools

    real_save = gtd_tools._save_list

    def _save(name: str, items: list[dict]) -> None:
        if name == doomed_list:
            raise _WriteWindowCrashError(name)
        real_save(name, items)

    monkeypatch.setattr(gtd_tools, "_save_list", _save)


def test_done_crash_in_the_write_window_duplicates_never_loses(monkeypatch):
    from skcapstone.mcp_tools import gtd_tools

    _seed("inbox", [{"id": "keep01", "text": "precious", "status": "inbox"}])
    _crash_on_saving(monkeypatch, "inbox")

    with pytest.raises(_WriteWindowCrashError):
        asyncio.run(gtd_tools._handle_gtd_done({"item_id": "keep01"}))

    assert [it["id"] for it in gtd_tools._load_list("inbox")] == ["keep01"]  # not lost
    assert [it["id"] for it in gtd_tools._load_list("archive")] == ["keep01"]  # duplicated


def test_move_crash_in_the_write_window_duplicates_never_loses(monkeypatch):
    from skcapstone.mcp_tools import gtd_tools

    _seed("inbox", [{"id": "keep02", "text": "precious", "status": "inbox"}])
    _crash_on_saving(monkeypatch, "inbox")

    with pytest.raises(_WriteWindowCrashError):
        asyncio.run(gtd_tools._handle_gtd_move({"item_id": "keep02", "destination": "next"}))

    assert [it["id"] for it in gtd_tools._load_list("inbox")] == ["keep02"]
    assert [it["id"] for it in gtd_tools._load_list("next-actions")] == ["keep02"]


def test_move_to_done_crash_in_the_write_window_duplicates_never_loses(monkeypatch):
    from skcapstone.mcp_tools import gtd_tools

    _seed("next-actions", [{"id": "keep03", "text": "precious", "status": "next"}])
    _crash_on_saving(monkeypatch, "next-actions")

    with pytest.raises(_WriteWindowCrashError):
        asyncio.run(gtd_tools._handle_gtd_move({"item_id": "keep03", "destination": "done"}))

    assert [it["id"] for it in gtd_tools._load_list("next-actions")] == ["keep03"]
    assert [it["id"] for it in gtd_tools._load_list("archive")] == ["keep03"]


def test_clarify_crash_in_the_write_window_duplicates_never_loses(monkeypatch):
    from skcapstone.mcp_tools import gtd_tools

    _seed("inbox", [{"id": "keep04", "text": "precious", "status": "inbox"}])
    _crash_on_saving(monkeypatch, "inbox")

    with pytest.raises(_WriteWindowCrashError):
        asyncio.run(
            gtd_tools._handle_gtd_clarify(
                {"item_id": "keep04", "actionable": True, "two_minute": False}
            )
        )

    assert [it["id"] for it in gtd_tools._load_list("inbox")] == ["keep04"]
    assert [it["id"] for it in gtd_tools._load_list("next-actions")] == ["keep04"]


def test_the_happy_path_still_transfers_exactly_once():
    from skcapstone.mcp_tools import gtd_tools

    _seed("inbox", [{"id": "ok01", "text": "normal", "status": "inbox"}])
    result = asyncio.run(gtd_tools._handle_gtd_done({"item_id": "ok01"}))
    assert json.loads(result[0].text)["done"] is True
    assert gtd_tools._load_list("inbox") == []
    assert [it["id"] for it in gtd_tools._load_list("archive")] == ["ok01"]
