"""ESTATE marker: a host never writes another estate's GTD store.

Covers the standalone (no skos) write path, which is what the chi fleet runs.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import skcapstone.mcp_tools._helpers as _helpers
from skcapstone.mcp_tools import gtd_tools as gt


@pytest.fixture(autouse=True)
def _standalone_store(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(_helpers, "SHARED_ROOT", str(tmp_path))
    monkeypatch.setattr(gt, "_HAVE_SKOS_SINK", False)
    monkeypatch.setattr(gt, "_skos_atomic_save", None)
    monkeypatch.setattr(gt, "_skos_store_lock", None)
    monkeypatch.setattr(gt, "_skos_capture", None)
    monkeypatch.delenv("SKWORLD_ESTATE", raising=False)
    monkeypatch.setenv("SK_GTD_HOST", "chiap08")
    return tmp_path


def _store() -> Path:
    return gt._gtd_dir()


def _estate(name: str) -> None:
    (_store() / "ESTATE").write_text(f"# owning estate\n{name}\n", encoding="utf-8")


def _capture(text: str = "t", ref: str = "r1"):
    return asyncio.run(
        gt._handle_gtd_capture({"text": text, "source": "manual", "source_ref": ref})
    )


def _inbox() -> list:
    return json.loads((_store() / "inbox.json").read_text(encoding="utf-8"))


def test_no_estate_file_is_unchanged():
    _capture()
    assert len(_inbox()) == 1


def test_same_estate_host_writes():
    _estate("chi")
    _capture()
    assert len(_inbox()) == 1


def test_other_estate_host_is_refused(monkeypatch):
    _estate("chi")
    monkeypatch.setenv("SK_GTD_HOST", "noroc2027")
    with pytest.raises(gt.EstateMismatchError):
        _capture()
    with pytest.raises(gt.EstateMismatchError):
        gt._save_list("next-actions", [{"id": "x"}])
    assert _inbox() == []


def test_env_and_aliases(monkeypatch):
    _estate("cakjr")
    monkeypatch.setenv("SK_GTD_HOST", "ziowk01")
    monkeypatch.setenv("SKWORLD_ESTATE", "chi")
    _capture()
    monkeypatch.setenv("SKWORLD_ESTATE", "chef")
    with pytest.raises(gt.EstateMismatchError):
        _capture(ref="r2")


def test_unknown_host_is_allowed(monkeypatch):
    _estate("nor")
    monkeypatch.setenv("SK_GTD_HOST", "ollama")
    _capture()
    assert len(_inbox()) == 1


def test_guard_only_reads_the_written_dir(tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    _estate("nor")  # the store is nor, but this write targets another dir
    gt._atomic_write_json(other / "x.json", [])
    assert json.loads((other / "x.json").read_text()) == []


def test_cross_estate_source_is_kept():
    _estate("chi")
    asyncio.run(
        gt._handle_gtd_capture(
            {"text": "needs Chef", "source": "nor-estate", "source_ref": "skmail:abc"}
        )
    )
    assert _inbox()[0]["source"] == "nor-estate"
