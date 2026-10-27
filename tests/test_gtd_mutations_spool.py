"""GTD clarify / move / done / reopen and the dreaming someday writer honor the
single-owner store (smilinTux/skos#35, #37; GTD follow-up 50a386282518).

The lists are whole-file JSON in a Syncthing-shared folder, so only the host in
``OWNER`` may rewrite them. On any other host these paths must append a spool
record for the owner and say so, never rewrite a list. On the owner, or with no
owner configured, or without skos, they must behave exactly as before.

CI may not have skos installed, so most tests stand in for its spool API; the
last test runs the real skos round trip when a new enough skos is importable.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers
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
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setenv("SKOS_ALLOW_EMPTY_STORE", "1")
    monkeypatch.setenv("SKAGENT", "lumina")
    monkeypatch.delenv("SK_GTD_DIR", raising=False)
    monkeypatch.delenv("SK_GTD_OWNER_HOST", raising=False)


class _FakeSkos:
    """Stands in for skos.gtd_ingest's owner/spool API."""

    def __init__(self, owner: bool, gtd_dir: Path):
        self.owner = owner
        self.dir = gtd_dir
        self.mutations: list[tuple] = []
        self.appends: list[tuple] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(gtd_tools, "_skos_is_owner", lambda: self.owner)
        monkeypatch.setattr(gtd_tools, "_skos_owner_host", lambda: "noroc2027")
        monkeypatch.setattr(gtd_tools, "_skos_gtd_dir", lambda: self.dir)
        monkeypatch.setattr(
            gtd_tools,
            "_skos_spool_mutation",
            lambda action, item_id, **kw: self.mutations.append((action, item_id, kw)),
        )
        monkeypatch.setattr(
            gtd_tools, "_skos_spool_append", lambda item, to: self.appends.append((item, to))
        )
        return self


def _no_skos(monkeypatch):
    for name in (
        "_skos_is_owner",
        "_skos_owner_host",
        "_skos_gtd_dir",
        "_skos_spool_append",
        "_skos_spool_mutation",
    ):
        monkeypatch.setattr(gtd_tools, name, None)


def _call(handler: str, **args) -> dict:
    return json.loads(asyncio.run(getattr(gtd_tools, handler)(args))[0].text)


def _seed() -> Path:
    d = gtd_tools._gtd_dir()
    gtd_tools._save_list("inbox", [{"id": "in1", "text": "call bob", "status": "inbox"}])
    gtd_tools._save_list("next-actions", [{"id": "nx1", "text": "ship it", "status": "next"}])
    gtd_tools._save_list(
        "archive",
        [
            {
                "id": "ar1",
                "text": "old",
                "status": "done",
                "completed_at": "2026-01-01T00:00:00+00:00",
            }
        ],
    )
    return d


def _snapshot(d: Path) -> dict[str, bytes]:
    return {f: (d / f).read_bytes() for f in STORE_FILES}


def _run_all_mutations():
    assert _call("_handle_gtd_clarify", item_id="in1", actionable=True, delegate_to="bob")["id"]
    assert _call("_handle_gtd_move", item_id="nx1", destination="someday")["id"]
    assert _call("_handle_gtd_reopen", item_id="ar1", destination="next")["id"]
    assert _call("_handle_gtd_done", item_id="in1")["id"]


def test_non_owner_spools_every_mutation_and_never_rewrites(monkeypatch):
    d = _seed()
    fake = _FakeSkos(owner=False, gtd_dir=d).install(monkeypatch)
    before = _snapshot(d)

    c = _call("_handle_gtd_clarify", item_id="in1", actionable=True, delegate_to="bob")
    m = _call("_handle_gtd_move", item_id="nx1", destination="done")
    r = _call("_handle_gtd_reopen", item_id="ar1", destination="next")
    k = _call("_handle_gtd_done", item_id="nx1")

    assert _snapshot(d) == before, "a non-owner host must not rewrite any list"
    for res in (c, m, r, k):
        assert res["queued"] is True and res["owner"] == "noroc2027"
        assert "queued for owner noroc2027" in res["message"]
    assert c["clarified"] is False and c["destination"] == "waiting-for"
    assert m["moved"] is False and m["to"] == "archive"
    assert r["reopened"] is False and r["to"] == "next-actions"
    assert k["done"] is False and "archive_count" not in k

    ops = {(a, i): kw for a, i, kw in fake.mutations}
    clar = ops[("clarify", "in1")]
    assert clar["to"] == "waiting-for" and clar["src"] == ["inbox"]
    assert clar["patch"]["delegate_to"] == "bob" and clar["patch"]["status"] == "waiting"
    assert clar["patch"]["clarified_at"] == clar["ts"]
    mv = ops[("move", "nx1")]
    assert mv["to"] == "archive" and mv["src"] == ["next-actions"]
    assert mv["patch"]["completed_at"] == mv["ts"]
    ro = ops[("reopen", "ar1")]
    assert ro["src"] == ["archive"] and ro["drop"] == ["completed_at"]
    dn = ops[("done", "nx1")]
    assert dn["to"] == "archive" and "archive" not in dn["src"]
    assert dn["patch"] == {"status": "done", "completed_at": dn["ts"]}


def test_non_owner_keeps_the_local_validation_errors(monkeypatch):
    d = _seed()
    fake = _FakeSkos(owner=False, gtd_dir=d).install(monkeypatch)
    assert (
        "not found in inbox"
        in _call("_handle_gtd_clarify", item_id="nx1", actionable=True)["error"]
    )
    assert "already archived" in _call("_handle_gtd_done", item_id="ar1")["error"]
    assert "not archived" in _call("_handle_gtd_reopen", item_id="nx1")["error"]
    assert "not found" in _call("_handle_gtd_move", item_id="ghost", destination="next")["error"]
    assert fake.mutations == []


def test_non_owner_journals_the_intended_post_state(monkeypatch):
    from skcapstone import gtd_journal

    d = _seed()
    _FakeSkos(owner=False, gtd_dir=d).install(monkeypatch)
    _call("_handle_gtd_done", item_id="nx1")
    (ev,) = gtd_journal.read_all()
    assert ev["action"] == "done" and ev["to"] == "archive" and ev["from"] == "next-actions"
    assert ev["item"]["status"] == "done" and ev["item"]["text"] == "ship it"


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("mode", ["owner", "skos_other_store"])
def test_owner_and_unrelated_store_are_byte_identical_to_no_skos(tmp_path, monkeypatch, mode):
    """On the owner (or when skos points at a different store, so its OWNER file
    says nothing about this one), the store bytes equal the pre-spool path."""
    monkeypatch.setattr(gtd_tools, "datetime", _FrozenDatetime)

    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path / "a"))
    _no_skos(monkeypatch)
    d_a = _seed()
    _run_all_mutations()
    baseline = _snapshot(d_a)

    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path / "b"))
    d_b = _seed()
    if mode == "owner":
        fake = _FakeSkos(owner=True, gtd_dir=d_b).install(monkeypatch)
    else:
        fake = _FakeSkos(owner=False, gtd_dir=tmp_path / "elsewhere").install(monkeypatch)
    _run_all_mutations()
    assert fake.mutations == []
    assert _snapshot(d_b) == baseline


def test_dreaming_spools_on_non_owner_and_writes_on_owner(tmp_path, monkeypatch):
    from skcapstone.dreaming import DreamingEngine, DreamResult

    result = DreamResult(
        dreamed_at=datetime(2026, 10, 27, tzinfo=timezone.utc),
        insights=["i1"],
        connections=["c1"],
        questions=["q1"],
    )
    gtd = tmp_path / "coordination" / "gtd"
    gtd.mkdir(parents=True)

    fake = _FakeSkos(owner=False, gtd_dir=gtd).install(monkeypatch)
    DreamingEngine(home=tmp_path, agent_name="lumina")._capture_to_gtd_someday(result)
    assert (
        not (gtd / "someday-maybe.json").exists()
        or json.loads((gtd / "someday-maybe.json").read_text()) == []
    )
    assert [to for _, to in fake.appends] == ["someday-maybe"] * 3
    assert all(it["id"] and it["source"] == "dreaming-engine" for it, _ in fake.appends)

    fake.owner = True
    DreamingEngine(home=tmp_path, agent_name="lumina")._capture_to_gtd_someday(result)
    assert len(json.loads((gtd / "someday-maybe.json").read_text())) == 3
    assert len(fake.appends) == 3


def test_dreaming_without_skos_writes_directly(tmp_path, monkeypatch):
    from skcapstone.dreaming import DreamingEngine, DreamResult

    _no_skos(monkeypatch)
    result = DreamResult(dreamed_at=datetime(2026, 10, 27, tzinfo=timezone.utc), insights=["i"])
    DreamingEngine(home=tmp_path, agent_name="lumina")._capture_to_gtd_someday(result)
    items = json.loads((tmp_path / "coordination" / "gtd" / "someday-maybe.json").read_text())
    assert [it["text"] for it in items] == ["[Dream insight] i"]


def test_real_skos_round_trip_non_owner_then_owner_drain(tmp_path, monkeypatch):
    """End to end with the real skos spool: nothing changes on the non-owner,
    the owner's drain lands every op, and a second drain is a no-op."""
    ingest = pytest.importorskip("skos.gtd_ingest")
    if not hasattr(ingest, "spool_mutation"):
        pytest.skip("installed skos predates spooled mutations (skos#37)")
    import importlib

    monkeypatch.setenv("SK_GTD_SPOOL_STATE", str(tmp_path / "state" / "offsets.json"))
    importlib.reload(gtd_tools)
    try:
        d = _seed()
        (d / "OWNER").write_text("noroc2027\n")
        before = _snapshot(d)

        monkeypatch.setenv("SK_GTD_HOST", "laptop41")
        _run_all_mutations()
        assert _snapshot(d) == before
        assert (d / "spool" / "laptop41.jsonl").exists()

        monkeypatch.setenv("SK_GTD_HOST", "noroc2027")
        assert ingest.drain_spool() == 4
        assert ingest.drain_spool() == 0

        def ids(name):
            return [it["id"] for it in gtd_tools._load_list(name)]

        assert ids("inbox") == [] and ids("waiting-for") == []
        assert ids("someday-maybe") == ["nx1"]
        assert ids("next-actions") == ["ar1"]
        assert ids("archive") == ["in1"]
    finally:
        importlib.reload(gtd_tools)
