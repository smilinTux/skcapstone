"""A blocked workspace must yield its dispatch slot.

Measured 2026-09-21 on chiap08: the same five cards were attempted 18 times
each over three hours out of a pool of 35 and the fleet launched nothing,
because WORKSPACE_BLOCKED logged and continued without recording any backoff.
"""

import ast
import json
import os
import time
from pathlib import Path

import pytest

ROTATE = Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"

NAMES = (
    "_workspace_cooldown_seconds",
    "_load_workspace_cooldown",
    "_record_workspace_cooldown",
    "_workspace_cooldown_active",
)
CONSTANTS = {
    "_WORKSPACE_COOLDOWN_DEFAULT_SECONDS",
    "_WORKSPACE_COOLDOWN_ENV",
}


def _load(tmp_path: Path) -> dict:
    """Load the cooldown helpers without executing the fleet launcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    wanted = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in NAMES
    }
    assert set(wanted) == set(NAMES), sorted(wanted)
    constants = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and {t.id for t in node.targets if isinstance(t, ast.Name)} & CONSTANTS
    ]
    assert {
        t.id for node in constants for t in node.targets if isinstance(t, ast.Name)
    } == CONSTANTS
    module = ast.Module(body=constants + [wanted[n] for n in NAMES], type_ignores=[])
    ns: dict = {"os": os, "json": json, "time": time}
    exec(compile(module, str(ROTATE), "exec"), ns)
    # Point the state file at the test's own directory.
    ns["_WORKSPACE_COOLDOWN_PATH"] = str(tmp_path / "workspace-cooldown.json")
    return ns


def test_default_window_is_one_hour(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.delenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", raising=False)
    assert ns["_workspace_cooldown_seconds"]() == 3600


def test_env_override_is_honoured(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "120")
    assert ns["_workspace_cooldown_seconds"]() == 120


@pytest.mark.parametrize("raw", ["", "   ", "abc", "-5"])
def test_bad_override_falls_back_rather_than_raising(tmp_path, monkeypatch, raw):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", raw)
    assert ns["_workspace_cooldown_seconds"]() == 3600


def test_a_blocked_card_is_skipped_then_released(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "100")
    ns["_record_workspace_cooldown"]("aaa11111", now=1000.0)
    assert ns["_workspace_cooldown_active"]("aaa11111", now=1050.0) is True
    # past the window the card returns to the pool on its own
    assert ns["_workspace_cooldown_active"]("aaa11111", now=1101.0) is False


def test_an_unrecorded_card_is_never_skipped(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "100")
    ns["_record_workspace_cooldown"]("aaa11111", now=1000.0)
    assert ns["_workspace_cooldown_active"]("bbb22222", now=1001.0) is False


def test_zero_window_disables_the_throttle(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "0")
    ns["_record_workspace_cooldown"]("aaa11111", now=1000.0)
    assert ns["_workspace_cooldown_active"]("aaa11111", now=1000.0) is False


def test_expired_entries_are_pruned_so_the_file_cannot_grow(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "100")
    ns["_record_workspace_cooldown"]("old00000", now=1000.0)
    ns["_record_workspace_cooldown"]("new00000", now=2000.0)
    data = json.loads(Path(ns["_WORKSPACE_COOLDOWN_PATH"]).read_text())
    assert "old00000" not in data
    assert "new00000" in data


@pytest.mark.parametrize("body", ["not json at all", "[]", '{"a": "not-a-number"}'])
def test_a_malformed_file_fails_open(tmp_path, monkeypatch, body):
    """A bug in a throttle must never be able to stop the fleet."""
    ns = _load(tmp_path)
    monkeypatch.setenv("SKFLEET_WORKSPACE_COOLDOWN_SECONDS", "100")
    Path(ns["_WORKSPACE_COOLDOWN_PATH"]).write_text(body, encoding="utf-8")
    assert ns["_load_workspace_cooldown"]() == {}
    assert ns["_workspace_cooldown_active"]("a", now=1.0) is False


def test_an_unwritable_path_does_not_raise(tmp_path, monkeypatch):
    ns = _load(tmp_path)
    ns["_WORKSPACE_COOLDOWN_PATH"] = "/proc/definitely/not/writable/cooldown.json"
    ns["_record_workspace_cooldown"]("aaa11111", now=1000.0)  # must not raise
    assert ns["_load_workspace_cooldown"]() == {}


def test_the_blocked_path_actually_records_the_cooldown():
    """Guards the wiring, not just the helpers."""
    source = ROTATE.read_text(encoding="utf-8")
    marker = 'log(d,"WORKSPACE_BLOCKED|%s|%s|%s"%(HOST,cid,exc))'
    assert marker in source
    tail = source[source.index(marker) : source.index(marker) + 400]
    assert "_record_workspace_cooldown(cid)" in tail, (
        "WORKSPACE_BLOCKED must record a cooldown or the card keeps its slot"
    )


def test_the_pool_filter_runs_before_the_lane_truncation():
    source = ROTATE.read_text(encoding="utf-8")
    pool_log = 'log(d,"POOL_IDS|%s|ids=%s"%(HOST,pool_ids))'
    sort_line = "pool.sort(key=lambda x:"
    assert pool_log in source and sort_line in source
    between = source[source.index(pool_log) : source.index(sort_line)]
    assert "_workspace_cooldown_active" in between
    assert "WORKSPACE_COOLDOWN_SKIPPED" in between
