"""SPE P1.5: no GTD write path bypasses the locked, atomic, deduped sink.

Card ``4082d990`` (sprint ``83482526``, epic ``373a33ca``). ``skcapstone gtd
capture`` inlined ``_load_list`` / ``_make_item`` / ``_save_list`` instead of
calling its own handler, so the CLI took NO lock and did NO dedupe: an
unserialized read-append-write that silently drops one of two concurrent
captures. The MCP path was fixed in card 272845a7; the CLI was left behind.

The skos ``mail.py`` writer is the other half of this card and is fixed in the
skos repo.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.cli.gtd import register_gtd_commands


@pytest.fixture(autouse=True)
def _isolate_gtd_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_gtd_commands(main)
    return main


def _capture(*args: str):
    return CliRunner().invoke(_main(), ["gtd", "capture", *args])


def _capture_in_thread(text: str) -> None:
    """Run the capture command body without CliRunner.

    CliRunner swaps ``sys.stdout`` process-wide, so two of them in flight at
    once tear each other's streams down. The concurrency properties live in the
    command body, not in click's parsing, so threads call the callback directly.
    """
    cmd = _main().commands["gtd"].commands["capture"]
    cmd.callback(text=text, source="manual", privacy="private", context=None, source_ref=None)


def test_cli_capture_serializes_on_the_shared_store_lock():
    """A CLI capture must WAIT for whoever holds the store lock.

    Fail-before: the CLI did its own read-append-write with no lock at all, so
    it sailed straight through and finished first.
    """
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

    def cli_write():
        _capture_in_thread("locked capture")
        order.append("cli-done")

    t_cli = threading.Thread(target=cli_write)
    t_cli.start()
    t_hold.join(3)
    t_cli.join(3)

    assert order == ["holder-release", "cli-done"], f"CLI bypassed the lock: {order}"


def test_cli_capture_dedupes_by_source_ref():
    """Routing through the handler means the CLI inherits whole-store dedupe."""
    from skcapstone.mcp_tools.gtd_tools import _load_list

    first = _capture("pay invoice", "--source", "email", "--source-ref", "gmail:t1")
    assert first.exit_code == 0, first.output
    second = _capture("pay invoice again", "--source", "email", "--source-ref", "gmail:t1")
    assert second.exit_code == 0, second.output
    assert "duplicate" in second.output.lower()

    inbox = _load_list("inbox")
    assert sum(1 for it in inbox if it.get("source_ref") == "gmail:t1") == 1


def test_cli_capture_still_captures_a_plain_quick_add():
    """The everyday path must be unchanged: no ref, always captured."""
    from skcapstone.mcp_tools.gtd_tools import _load_list

    result = _capture("buy milk", "--context", "@errands")
    assert result.exit_code == 0, result.output
    assert "Captured" in result.output

    inbox = _load_list("inbox")
    assert len(inbox) == 1
    assert inbox[0]["text"] == "buy milk"
    assert inbox[0]["context"] == "@errands"
    assert inbox[0]["source"] == "manual"
    assert inbox[0]["id"] in result.output


def test_cli_capture_reports_a_write_failure_instead_of_claiming_success(monkeypatch):
    """A refused capture must not print 'Captured!'."""
    from skcapstone.mcp_tools import gtd_tools

    async def _refuse(_args):
        return gtd_tools._error_response("store is on fire")

    monkeypatch.setattr(gtd_tools, "_handle_gtd_capture", _refuse)
    result = _capture("doomed")
    assert result.exit_code != 0
    assert "Captured" not in result.output
    assert "store is on fire" in result.output


def test_concurrent_cli_captures_cannot_lose_an_item():
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir, _load_list

    _gtd_dir()
    threads = [threading.Thread(target=_capture_in_thread, args=(f"item {i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    texts = {it["text"] for it in _load_list("inbox")}
    assert texts == {f"item {i}" for i in range(8)}


def test_the_store_stays_valid_json_after_the_concurrent_run():
    from skcapstone.mcp_tools.gtd_tools import _gtd_dir

    _gtd_dir()
    threads = [threading.Thread(target=_capture_in_thread, args=(f"x{i}",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    raw = (_gtd_dir() / "inbox.json").read_text(encoding="utf-8")
    assert isinstance(json.loads(raw), list)
