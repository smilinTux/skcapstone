"""Tests for the fleet worker activity stream (scripts/fleet/skfleet-worker-stream.py).

No systemd and no live node is contacted: unit enumeration is monkeypatched
and the session files are fixtures written into tmp_path. The two regression
tests at the bottom cover the defects that made an earlier version of this
projection lie about worker liveness.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fleet" / "skfleet-worker-stream.py"


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location("skfleet_worker_stream", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(role: str, text: str = "", tool: str | None = None, ts: str = "2026-09-19T00:00:00Z"):
    content: list[dict] = []
    if tool:
        content.append({"type": "tool_use", "name": tool, "input": {"command": "x" * 20000}})
    if text:
        content.append({"type": "text", "text": text})
    return json.dumps({"timestamp": ts, "message": {"role": role, "content": content}})


def _session(root: Path, card: str, events: list[str]) -> Path:
    d = root / f"--home-skuser01-workspace-{card}--"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "20260919_abc.jsonl"
    path.write_text("".join(e + "\n" for e in events))
    return path


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------


def test_project_keeps_role_tool_and_preview(mod):
    row = mod.project(_event("assistant", "hello   world", tool="bash"))
    assert row["role"] == "assistant"
    assert row["tool"] == "bash"
    assert row["preview"] == "hello world"
    assert row["ts"] == "2026-09-19T00:00:00Z"


def test_project_drops_tool_payload(mod):
    """The whole point of a projection: the 20 KB tool input must not survive."""
    raw = _event("assistant", "ok", tool="bash")
    assert len(raw) > 20000
    row = mod.project(raw)
    assert len(json.dumps(row)) < 400
    assert "xxxxxxxxxx" not in json.dumps(row)


def test_project_truncates_long_preview(mod):
    row = mod.project(_event("assistant", "y" * 5000))
    assert len(row["preview"]) == mod.PREVIEW


def test_project_returns_none_on_garbage(mod):
    assert mod.project("not json at all") is None


def test_project_handles_string_content(mod):
    raw = json.dumps({"timestamp": "t", "message": {"role": "user", "content": "plain"}})
    assert mod.project(raw)["preview"] == "plain"


# --------------------------------------------------------------------------
# Tail: byte offsets and partial lines
# --------------------------------------------------------------------------


def test_tail_reads_only_new_lines(mod, tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n')
    tail = mod.Tail(str(p), start_at_end=True)
    assert tail.read_lines() == []
    with p.open("a") as fh:
        fh.write('{"a":2}\n')
    assert tail.read_lines() == ['{"a":2}']


def test_tail_from_start_reads_history(mod, tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n')
    assert mod.Tail(str(p), start_at_end=False).read_lines() == ['{"a":1}', '{"a":2}']


def test_tail_holds_back_partial_line(mod, tmp_path):
    """A poll can land mid-append. A truncated event must never be emitted."""
    p = tmp_path / "s.jsonl"
    p.write_text("")
    tail = mod.Tail(str(p), start_at_end=True)
    with p.open("a") as fh:
        fh.write('{"a":1}\n{"par')
    assert tail.read_lines() == ['{"a":1}']
    with p.open("a") as fh:
        fh.write('tial":2}\n')
    assert tail.read_lines() == ['{"partial":2}']


def test_tail_offset_is_byte_exact_with_multibyte(mod, tmp_path):
    """Byte offsets, not character counts.

    Seeking a text handle to a byte offset and advancing it by the length of
    the re-encoded string drifts the moment an event carries a multi-byte
    character, and tool output routinely does.
    """
    p = tmp_path / "s.jsonl"
    p.write_text("")
    tail = mod.Tail(str(p), start_at_end=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"t":"café ✅ 日本語"}\n')
    assert tail.read_lines() == ['{"t":"café ✅ 日本語"}']
    with p.open("a", encoding="utf-8") as fh:
        fh.write('{"t":"next"}\n')
    assert tail.read_lines() == ['{"t":"next"}']


def test_tail_restarts_if_file_shrinks(mod, tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"a":1}\n{"a":2}\n')
    tail = mod.Tail(str(p), start_at_end=True)
    p.write_text('{"b":1}\n')
    assert tail.read_lines() == ['{"b":1}']


# --------------------------------------------------------------------------
# session selection and snapshot
# --------------------------------------------------------------------------


def test_snapshot_reports_events_and_silence(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SESSION_ROOT", str(tmp_path))
    path = _session(tmp_path, "aabbccdd", [_event("assistant", "hi")] * 3)
    old = time.time() - 300
    os.utime(path, (old, old))
    monkeypatch.setattr(
        mod, "active_workers", lambda: [("aabbccdd", "skfleet-worker-sk-m-aabbccdd.service", None)]
    )
    (row,) = mod.snapshot("chiap01")
    assert row["card"] == "aabbccdd"
    assert row["events"] == 3
    assert 290 <= row["silent_s"] <= 400
    assert row["state"] == "active"


def test_snapshot_flags_worker_with_no_session_for_this_run(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SESSION_ROOT", str(tmp_path))
    monkeypatch.setattr(
        mod, "active_workers", lambda: [("deadbee1", "skfleet-worker-sk-m-deadbee1.service", None)]
    )
    (row,) = mod.snapshot("chiap01")
    assert row["events"] == 0
    assert row["silent_s"] is None
    assert "no session file" in row["note"]


# --------------------------------------------------------------------------
# REGRESSIONS. Both of these produced false "worker is dead" readings.
# --------------------------------------------------------------------------


def test_session_from_a_previous_run_is_refused(mod, tmp_path, monkeypatch):
    """A card worked before leaves sessions the glob still matches.

    Taking the newest unconditionally reported a worker that had just started
    as silent for 8.4 days, because the match belonged to an earlier attempt.
    """
    monkeypatch.setattr(mod, "SESSION_ROOT", str(tmp_path))
    path = _session(tmp_path, "25ab78c6", [_event("assistant", "old run")])
    stale = time.time() - 8.4 * 86400
    os.utime(path, (stale, stale))
    just_started = time.time() - 30
    assert mod.session_file("25ab78c6", just_started) is None
    # ... and is accepted for a unit that really is that old
    assert mod.session_file("25ab78c6", stale - 60) == str(path)


def test_session_accepted_within_run_skew(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SESSION_ROOT", str(tmp_path))
    path = _session(tmp_path, "aabbccdd", [_event("assistant", "x")])
    started = os.path.getmtime(path) + mod.RUN_SKEW_S - 10
    assert mod.session_file("aabbccdd", started) == str(path)


def test_active_workers_never_includes_failed_units(mod, monkeypatch):
    """`list-units` without a state filter includes FAILED units.

    Counting skfleet-worker-glm-l-25ab78c6-repair.service, failed on chiap02
    since 2026-09-10 with MainPID=0, made an 8.5-day-dead unit read as a live
    worker gone silent.
    """
    seen: list[tuple[str, ...]] = []

    def fake(*args):
        seen.append(args)
        if "--state=active" in args:
            return "skfleet-worker-sk-m-aabbccdd.service loaded active running w\n"
        if "--state=failed" in args:
            return "skfleet-worker-glm-l-25ab78c6-repair.service loaded failed failed w\n"
        raise AssertionError(f"unfiltered list-units call: {args}")

    monkeypatch.setattr(mod, "_systemctl", fake)
    monkeypatch.setattr(mod, "unit_started_at", lambda unit: None)

    assert [c for c, _u, _s in mod.active_workers()] == ["aabbccdd"]
    assert mod.failed_workers() == [
        {
            "unit": "skfleet-worker-glm-l-25ab78c6-repair.service",
            "card": "25ab78c6",
            "state": "failed",
        }
    ]
    # every enumeration was state-filtered
    assert all(any(a.startswith("--state=") for a in args) for args in seen)


def test_card_id_is_the_last_hex_run_in_the_unit_name(mod, monkeypatch):
    monkeypatch.setattr(
        mod,
        "_systemctl",
        lambda *a: "skfleet-worker-glm-l-25ab78c6-repair.service loaded failed failed w\n",
    )
    assert mod.failed_workers()[0]["card"] == "25ab78c6"
