"""SPE P1.2: gtd reopen restores an archived item under its ORIGINAL id.

Card ``0ef48ec9`` (sprint ``83482526``, epic ``373a33ca``). ``done`` was a
one-way door: there was no undo anywhere, and ``move`` could not even see
archived items. The 2026-08-13 recovery meant hand-reading ``archive.json`` and
recapturing under a NEW id, which breaks the chain: every reference to the old
id, and the item's whole history, is orphaned.

``reopen`` is the reversal, and it is a reversal in the SPE sense: one more
appended event whose effect undoes the previous one. No stored event is edited,
nothing is deleted from the journal, and the item keeps its id.

The prior list comes from the journal (P1.1), which is precisely what the
journal was built to make possible.
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
    monkeypatch.setenv("SKAGENT", "lumina")


def _run(handler, args) -> dict:
    return json.loads(asyncio.run(handler(args))[0].text)


def _capture(text: str, **kw) -> str:
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_capture

    return _run(_handle_gtd_capture, {"text": text, **kw})["id"]


def _done(item_id: str) -> dict:
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_done

    return _run(_handle_gtd_done, {"item_id": item_id})


def _reopen(item_id: str, **kw) -> dict:
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_reopen

    return _run(_handle_gtd_reopen, {"item_id": item_id, **kw})


def test_reopen_restores_the_item_under_the_same_id():
    from skcapstone.mcp_tools.gtd_tools import _load_list

    item_id = _capture("closed too early")
    _done(item_id)
    assert _load_list("inbox") == []

    result = _reopen(item_id)
    assert result["reopened"] is True
    assert result["id"] == item_id  # SAME id, not a recapture
    assert result["to"] == "inbox"
    assert [it["id"] for it in _load_list("inbox")] == [item_id]
    assert _load_list("archive") == []


def test_reopen_returns_the_item_to_the_list_it_came_from():
    """Not always the inbox: the journal knows where it actually was."""
    from skcapstone.mcp_tools.gtd_tools import _handle_gtd_move, _load_list

    item_id = _capture("was a next action")
    asyncio.run(_handle_gtd_move({"item_id": item_id, "destination": "waiting"}))
    _done(item_id)

    result = _reopen(item_id)
    assert result["to"] == "waiting-for"
    assert [it["id"] for it in _load_list("waiting-for")] == [item_id]


def test_reopen_clears_the_completion_and_restores_the_status():
    from skcapstone.mcp_tools.gtd_tools import _load_list

    item_id = _capture("undo me")
    _done(item_id)
    _reopen(item_id)

    item = _load_list("inbox")[0]
    assert item["status"] == "inbox"
    assert not item.get("completed_at")
    assert item["reopened_at"]


def test_reopen_is_a_reversing_event_not_an_edit_of_history():
    from skcapstone import gtd_journal

    item_id = _capture("undo me")
    _done(item_id)
    before = [e["event_id"] for e in gtd_journal.read_all()]
    _reopen(item_id)
    after = gtd_journal.read_all()

    # The prior events are all still there, byte-for-byte, in the same order.
    assert [e["event_id"] for e in after][: len(before)] == before
    # And the reversal is one MORE event, pointing the other way.
    last = after[-1]
    assert last["action"] == "reopen"
    assert last["from"] == "archive"
    assert last["to"] == "inbox"
    assert last["item_id"] == item_id


def test_reopen_folds_back_to_the_pre_done_state():
    from skcapstone import gtd_journal
    from skcapstone.mcp_tools.gtd_tools import GTD_FILES, _load_list

    item_id = _capture("round trip")
    _done(item_id)
    _reopen(item_id)

    folded = gtd_journal.fold()
    live = {name: _load_list(name) for name in GTD_FILES}
    assert {k: [it["id"] for it in v] for k, v in folded.items()} == {
        k: [it["id"] for it in v] for k, v in live.items()
    }
    assert [it["id"] for it in folded["inbox"]] == [item_id]


def test_reopen_accepts_an_explicit_destination():
    from skcapstone.mcp_tools.gtd_tools import _load_list

    item_id = _capture("put it somewhere else")
    _done(item_id)
    result = _reopen(item_id, destination="next")
    assert result["to"] == "next-actions"
    assert [it["id"] for it in _load_list("next-actions")] == [item_id]
    assert _load_list("inbox") == []


def test_reopen_rejects_an_item_that_is_not_archived():
    item_id = _capture("still live")
    result = _reopen(item_id)
    assert result.get("error")


def test_reopen_rejects_an_unknown_id():
    assert _reopen("nosuchid").get("error")


def test_reopen_writes_the_destination_before_dropping_the_archive(monkeypatch):
    """Same crash-window rule as every other transfer (P1.3)."""
    from skcapstone.mcp_tools import gtd_tools

    item_id = _capture("precious")
    _done(item_id)

    real_save = gtd_tools._save_list

    def _save(name, items):
        if name == "archive":
            raise OSError("crash between the two saves")
        real_save(name, items)

    monkeypatch.setattr(gtd_tools, "_save_list", _save)
    with pytest.raises(OSError):
        _reopen(item_id)

    assert [it["id"] for it in gtd_tools._load_list("archive")] == [item_id]
    assert [it["id"] for it in gtd_tools._load_list("inbox")] == [item_id]


def test_reopen_is_exposed_as_a_tool_and_a_cli_verb():
    import click
    from click.testing import CliRunner

    from skcapstone.cli.gtd import register_gtd_commands
    from skcapstone.mcp_tools.gtd_tools import HANDLERS, TOOLS

    assert "gtd_reopen" in HANDLERS
    assert any(t.name == "gtd_reopen" for t in TOOLS)

    item_id = _capture("via the cli")
    _done(item_id)

    @click.group()
    def main():
        pass

    register_gtd_commands(main)
    result = CliRunner().invoke(main, ["gtd", "reopen", item_id])
    assert result.exit_code == 0, result.output
    assert item_id in result.output

    from skcapstone.mcp_tools.gtd_tools import _load_list

    assert [it["id"] for it in _load_list("inbox")] == [item_id]
