"""Standalone skcapstone (no skos) honors the GTD OWNER file.

chi runs skcapstone without skos, so before this every chi host rewrote the
whole-file GTD lists and Syncthing forked them (31 conflict copies on chiap08).
skcapstone.gtd_spool is the skos#35/#37 mechanism with the same record format:
non-owner hosts append to spool/<host>.jsonl, the OWNER host drains.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone import gtd_spool
from skcapstone.mcp_tools import gtd_tools

STORE_FILES = (
    "inbox.json",
    "next-actions.json",
    "projects.json",
    "waiting-for.json",
    "someday-maybe.json",
    "archive.json",
)


@pytest.fixture(autouse=True)
def _standalone(tmp_path: Path, monkeypatch):
    """Isolated store, and gtd_tools bound to skcapstone's spool even where skos
    is installed (noroc2027), so these tests exercise the chi code path."""
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.setenv("SK_GTD_SPOOL_STATE", str(tmp_path / "state" / "offsets.json"))
    monkeypatch.setenv("SKWORLD_ESTATE", "chi")
    for var in ("SK_GTD_DIR", "SK_GTD_OWNER_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(gtd_tools, "_SPOOL_IMPL", "skcapstone")
    monkeypatch.setattr(gtd_tools, "_HAVE_SKOS_SINK", False)
    monkeypatch.setattr(gtd_tools, "_skos_atomic_save", None)
    monkeypatch.setattr(gtd_tools, "_skos_store_lock", None)
    monkeypatch.setattr(gtd_tools, "_skos_gtd_dir", gtd_spool.gtd_dir)
    monkeypatch.setattr(gtd_tools, "_skos_is_owner", gtd_spool.is_owner)
    monkeypatch.setattr(gtd_tools, "_skos_owner_host", gtd_spool.owner_host)
    monkeypatch.setattr(gtd_tools, "_skos_spool_append", gtd_spool.spool_append)
    monkeypatch.setattr(gtd_tools, "_skos_spool_mutation", gtd_spool.spool_mutation)


def _store() -> Path:
    return gtd_tools._gtd_dir()


def _set_owner(name: str = "chiap08") -> None:
    (_store() / "OWNER").write_text(f"# the one writer\n{name}\n", encoding="utf-8")


def _as_host(monkeypatch, host: str) -> None:
    monkeypatch.setenv("SK_GTD_HOST", host)


def _call(handler: str, **args) -> dict:
    return json.loads(asyncio.run(getattr(gtd_tools, handler)(args))[0].text)


def _lists() -> dict[str, list]:
    d = _store()
    return {f: json.loads((d / f).read_text(encoding="utf-8")) for f in STORE_FILES}


def _snapshot() -> dict[str, bytes]:
    return {f: (_store() / f).read_bytes() for f in STORE_FILES}


def _spool(host: str) -> list[dict]:
    p = _store() / "spool" / f"{host}.jsonl"
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def test_no_owner_file_writes_directly(monkeypatch):
    _as_host(monkeypatch, "chiap04")
    out = _call("_handle_gtd_capture", text="buy milk")
    assert out["captured"] is True and "queued" not in out
    assert [i["id"] for i in _lists()["inbox.json"]] == [out["id"]]
    assert not (_store() / "spool").exists()


def test_non_owner_capture_spools_and_owner_drain_lands_it_once(monkeypatch):
    _set_owner()
    _as_host(monkeypatch, "chiap04")
    before = _snapshot()
    out = _call("_handle_gtd_capture", text="fix syncthing", source_ref="ref-1")
    assert out["queued"] is True and out["owner"] == "chiap08"
    assert _snapshot() == before
    rec = _spool("chiap04")[0]
    assert rec["op"] == "capture" and rec["id"] == out["id"] and rec["v"] == 1
    assert rec["capture"]["source_ref"] == "ref-1"
    # same ref again on the same host: dedupe against its own spool
    assert _call("_handle_gtd_capture", text="fix syncthing", source_ref="ref-1")["duplicate"]

    _as_host(monkeypatch, "chiap08.tailnet")  # short name matches OWNER
    assert gtd_spool.drain_spool() == 1
    assert [i["id"] for i in _lists()["inbox.json"]] == [out["id"]]
    assert gtd_spool.drain_spool() == 0
    (Path(gtd_spool._offsets_path())).unlink()  # lost offsets: replay is idempotent
    assert gtd_spool.drain_spool() == 0
    assert len(_lists()["inbox.json"]) == 1


def test_non_owner_mutations_spool_and_replay(monkeypatch):
    _as_host(monkeypatch, "chiap08")
    gtd_tools._save_list("inbox", [{"id": "in1", "text": "call bob", "status": "inbox"}])
    gtd_tools._save_list("next-actions", [{"id": "nx1", "text": "ship", "status": "next"}])
    gtd_tools._save_list(
        "archive",
        [{"id": "ar1", "text": "old", "status": "done", "completed_at": "2026-01-01T00:00:00Z"}],
    )
    _set_owner()
    _as_host(monkeypatch, "chiwk13-wsl")
    before = _snapshot()
    assert _call("_handle_gtd_clarify", item_id="in1", actionable=True, delegate_to="bob")[
        "queued"
    ]
    assert _call("_handle_gtd_done", item_id="nx1")["queued"]
    assert _call("_handle_gtd_reopen", item_id="ar1", destination="next")["queued"]
    assert _snapshot() == before
    assert [r["op"] for r in _spool("chiwk13-wsl")] == ["clarify", "done", "reopen"]

    _as_host(monkeypatch, "chiap08")
    assert gtd_spool.drain_spool() == 3
    lists = _lists()
    assert [i["id"] for i in lists["waiting-for.json"]] == ["in1"]
    assert lists["waiting-for.json"][0]["delegate_to"] == "bob"
    assert {i["id"] for i in lists["next-actions.json"]} == {"ar1"}
    assert [i["id"] for i in lists["archive.json"]] == ["nx1"]
    assert lists["inbox.json"] == []


def test_legacy_list_rewrite_is_spooled_on_non_owner(monkeypatch):
    """The skcoord ITIL fallback sink and _complete_gtd_items write lists via
    _load_list/_save_list/_remove_item_from_list/_save_archive directly."""
    _set_owner()
    _as_host(monkeypatch, "chiap01")
    before = _snapshot()
    item = gtd_tools._make_item(text="[ITIL:inc-1] down", source="itil", context="@ops")
    item["source_ref"] = "inc-1"
    inbox = gtd_tools._load_list("inbox")
    inbox.append(item)
    gtd_tools._save_list("inbox", inbox)
    assert _snapshot() == before
    assert _spool("chiap01")[0]["op"] == "append"

    _as_host(monkeypatch, "chiap08")
    assert gtd_spool.drain_spool() == 1
    assert [i["id"] for i in _lists()["inbox.json"]] == [item["id"]]

    # a second host raising the same incident: one item, not two
    _as_host(monkeypatch, "chiap02")
    dup = {**gtd_tools._make_item(text="[ITIL:inc-1] down", source="itil"), "source_ref": "inc-1"}
    gtd_tools._save_list("inbox", [*gtd_tools._load_list("inbox"), dup])

    # resolve on a non-owner: remove from inbox, then append to archive
    _as_host(monkeypatch, "chiap01")
    src, found, _ = gtd_tools._find_item_across_lists(item["id"])
    gtd_tools._remove_item_from_list(src, item["id"])
    found["status"] = "done"
    found["completed_at"] = "2026-10-28T00:00:00+00:00"
    archive = gtd_tools._load_archive()
    archive.append(found)
    gtd_tools._save_archive(archive)
    assert [r["op"] for r in _spool("chiap01")] == ["append", "done"]

    _as_host(monkeypatch, "chiap08")
    assert gtd_spool.drain_spool() == 1
    lists = _lists()
    assert lists["inbox.json"] == []
    assert [(i["id"], i["status"]) for i in lists["archive.json"]] == [(item["id"], "done")]


def test_owner_write_drains_spool_first(monkeypatch):
    _set_owner()
    _as_host(monkeypatch, "chiap03")
    spooled = _call("_handle_gtd_capture", text="from chiap03")["id"]
    _as_host(monkeypatch, "chiap08")
    own = _call("_handle_gtd_capture", text="from the owner")
    assert "queued" not in own
    assert [i["id"] for i in _lists()["inbox.json"]] == [spooled, own["id"]]


def test_dreaming_someday_spools_on_non_owner(monkeypatch):
    _set_owner()
    _as_host(monkeypatch, "chiap04")
    gtd_tools._spool_owner(_store())  # resolves without raising
    item = {"id": "dr1", "text": "[Dream insight] x", "status": "someday"}
    owner = gtd_tools._spool_owner(_store())
    assert owner == "chiap08"
    gtd_tools._skos_spool_append(item, "someday-maybe")
    _as_host(monkeypatch, "chiap08")
    assert gtd_spool.drain_spool() == 1
    assert [i["id"] for i in _lists()["someday-maybe.json"]] == ["dr1"]


def test_upsert_spools_then_reconciles(monkeypatch):
    _set_owner()
    cap = gtd_spool.GtdCapture(
        text="waiting on Casey", source="coord-human", source_ref="card-1", status="waiting"
    )
    _as_host(monkeypatch, "chiap02")
    item_id, action = gtd_spool.upsert(cap)
    assert action == "spooled"
    _as_host(monkeypatch, "chiap08")
    assert gtd_spool.drain_spool() == 1
    assert gtd_spool.upsert(cap) == (item_id, "unchanged")
    done = gtd_spool.GtdCapture(
        text="waiting on Casey", source="coord-human", source_ref="card-1", status="done"
    )
    assert gtd_spool.upsert(done) == (item_id, "completed")
    assert [i["id"] for i in _lists()["archive.json"]] == [item_id]


def test_corrupt_list_is_never_rewritten_by_the_drain(monkeypatch):
    _set_owner()
    _as_host(monkeypatch, "chiap04")
    _call("_handle_gtd_capture", text="x")
    (_store() / "inbox.json").write_text("{not json", encoding="utf-8")
    _as_host(monkeypatch, "chiap08")
    with pytest.raises(gtd_spool.CorruptStoreError):
        gtd_spool.drain_spool()
    assert (_store() / "inbox.json").read_text(encoding="utf-8") == "{not json"


def test_drain_spool_cli(monkeypatch):
    from skcapstone.cli import main

    monkeypatch.setitem(__import__("sys").modules, "skos.gtd_ingest", None)
    runner = CliRunner()
    _as_host(monkeypatch, "chiap04")
    assert "no GTD OWNER" in runner.invoke(main, ["gtd", "drain-spool"]).output
    _set_owner()
    _call("_handle_gtd_capture", text="queued one")
    res = runner.invoke(main, ["gtd", "drain-spool"])
    assert res.exit_code == 0 and "not the GTD owner (owner is chiap08)" in res.output
    _as_host(monkeypatch, "chiap08")
    res = runner.invoke(main, ["gtd", "drain-spool"])
    assert res.exit_code == 0 and "applied 1 spooled op(s)" in res.output


def test_records_replay_on_skos(monkeypatch, tmp_path):
    """Compatibility: a skos owner applies spool records skcapstone wrote."""
    skos_gtd = pytest.importorskip("skos.gtd_ingest")
    if not hasattr(skos_gtd, "spool_mutation"):
        pytest.skip("skos too old for the mutation spool")
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    monkeypatch.setenv("SK_GTD_DIR", str(_store()))
    gtd_tools._save_list("inbox", [{"id": "in1", "text": "a", "status": "inbox"}])
    _set_owner()
    _as_host(monkeypatch, "chiap04")
    new_id = _call("_handle_gtd_capture", text="from chi")["id"]
    _call("_handle_gtd_done", item_id="in1")
    _as_host(monkeypatch, "chiap08")
    assert skos_gtd.drain_spool() == 2
    lists = _lists()
    assert [i["id"] for i in lists["inbox.json"]] == [new_id]
    assert [i["id"] for i in lists["archive.json"]] == ["in1"]


def test_drain_units_ship():
    root = Path(gtd_spool.__file__).parent / "data" / "systemd"
    svc = (root / "skcapstone-gtd-drain.service").read_text(encoding="utf-8")
    timer = (root / "skcapstone-gtd-drain.timer").read_text(encoding="utf-8")
    assert "skcapstone gtd drain-spool" in svc
    assert "OnCalendar=*:0/10" in timer
