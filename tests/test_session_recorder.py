"""Tests for session_recorder and session_replayer."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# SessionRecorder
# ---------------------------------------------------------------------------


class TestSessionRecorder:
    def test_auto_session_file_created(self, tmp_agent_home: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder

        rec = SessionRecorder.start_session(tmp_agent_home)
        assert rec.auto_path is not None
        assert rec.auto_path.exists()
        rec.close()

    def test_record_writes_jsonl_line(self, tmp_agent_home: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder

        rec = SessionRecorder.start_session(tmp_agent_home)
        rec.record(
            tool="memory_store",
            arguments={"content": "hello", "tags": ["test"]},
            result=[{"type": "text", "text": '{"ok": true}'}],
            duration_ms=12,
        )
        rec.close()

        lines = rec.auto_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["tool"] == "memory_store"
        assert entry["duration_ms"] == 12
        assert entry["arguments"]["content"] == "hello"
        assert "ts" in entry

    def test_explicit_output_file(self, tmp_agent_home: Path, tmp_path: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder

        out = tmp_path / "explicit.jsonl"
        rec = SessionRecorder.start_session(tmp_agent_home, output_path=out)
        rec.record("coord_status", {}, [{"type": "text", "text": "ok"}], 5)
        rec.close()

        assert out.exists()
        entry = json.loads(out.read_text(encoding="utf-8").strip())
        assert entry["tool"] == "coord_status"

    def test_env_var_output_file(
        self, tmp_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from skcapstone.session_recorder import SessionRecorder

        out = tmp_path / "env_output.jsonl"
        monkeypatch.setenv("SKCAPSTONE_RECORD_FILE", str(out))
        rec = SessionRecorder.start_session(tmp_agent_home)
        rec.record("agent_status", {}, [], 1)
        rec.close()

        assert out.exists()
        entry = json.loads(out.read_text(encoding="utf-8").strip())
        assert entry["tool"] == "agent_status"

    def test_auto_rotate_keeps_last_five(self, tmp_agent_home: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder, list_sessions

        # Create 7 sessions
        for _ in range(7):
            rec = SessionRecorder.start_session(tmp_agent_home)
            rec.record("agent_status", {}, [], 1)
            rec.close()

        remaining = list_sessions(tmp_agent_home)
        assert len(remaining) == 5

    def test_count_tracks_records(self, tmp_agent_home: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder

        rec = SessionRecorder.start_session(tmp_agent_home)
        assert rec.count == 0
        for i in range(3):
            rec.record(f"tool_{i}", {}, [], i)
        assert rec.count == 3
        rec.close()

    def test_multiple_records_multiple_lines(self, tmp_agent_home: Path) -> None:
        from skcapstone.session_recorder import SessionRecorder, load_session

        rec = SessionRecorder.start_session(tmp_agent_home)
        tools = ["memory_store", "coord_status", "agent_status"]
        for t in tools:
            rec.record(t, {}, [], 1)
        rec.close()

        entries = load_session(rec.auto_path)
        assert [e["tool"] for e in entries] == tools


# ---------------------------------------------------------------------------
# load_session / list_sessions helpers
# ---------------------------------------------------------------------------


class TestSessionHelpers:
    def test_load_session_empty_file(self, tmp_path: Path) -> None:
        from skcapstone.session_recorder import load_session

        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert load_session(f) == []

    def test_load_session_skips_bad_lines(self, tmp_path: Path) -> None:
        from skcapstone.session_recorder import load_session

        f = tmp_path / "bad.jsonl"
        f.write_text(
            '{"tool": "ok", "arguments": {}, "result": [], "duration_ms": 1, "ts": "x"}\n'
            "not-json\n"
            '{"tool": "ok2", "arguments": {}, "result": [], "duration_ms": 2, "ts": "y"}\n'
        )
        entries = load_session(f)
        assert len(entries) == 2
        assert entries[0]["tool"] == "ok"
        assert entries[1]["tool"] == "ok2"

    def test_list_sessions_newest_first(self, tmp_agent_home: Path) -> None:
        import time
        from skcapstone.session_recorder import SessionRecorder, list_sessions

        recs = []
        for i in range(3):
            rec = SessionRecorder.start_session(tmp_agent_home)
            rec.record("t", {}, [], i)
            rec.close()
            recs.append(rec.auto_path)
            time.sleep(0.01)  # ensure distinct mtime

        listed = list_sessions(tmp_agent_home)
        assert len(listed) == 3
        # newest first
        assert listed[0].stat().st_mtime >= listed[1].stat().st_mtime


# ---------------------------------------------------------------------------
# SessionReplayer — dry-run
# ---------------------------------------------------------------------------


class TestSessionReplayerDryRun:
    def _write_session(self, path: Path, entries: list[dict]) -> None:
        with path.open("w") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")

    def test_dry_run_yields_results(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import SessionReplayer

        f = tmp_path / "s.jsonl"
        self._write_session(f, [
            {"ts": "t", "tool": "agent_status", "arguments": {}, "result": [], "duration_ms": 5},
            {"ts": "t", "tool": "coord_status", "arguments": {}, "result": [], "duration_ms": 3},
        ])

        replayer = SessionReplayer(f, dry_run=True)
        results = list(replayer.replay())
        assert len(results) == 2
        assert results[0].tool == "agent_status"
        assert results[1].tool == "coord_status"

    def test_dry_run_no_execution(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import SessionReplayer

        f = tmp_path / "s.jsonl"
        self._write_session(f, [
            {"ts": "t", "tool": "agent_status", "arguments": {}, "result": [], "duration_ms": 5},
        ])

        replayer = SessionReplayer(f, dry_run=True)
        results = list(replayer.replay())
        r = results[0]
        assert r.replayed_result is None
        assert r.match is None
        assert r.duration_ms == 0
        assert r.error is None

    def test_dry_run_empty_file(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import SessionReplayer

        f = tmp_path / "empty.jsonl"
        f.write_text("")

        results = list(SessionReplayer(f, dry_run=True).replay())
        assert results == []

    def test_dry_run_arguments_preserved(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import SessionReplayer

        args = {"content": "test memory", "tags": ["debug"]}
        f = tmp_path / "s.jsonl"
        self._write_session(f, [
            {"ts": "t", "tool": "memory_store", "arguments": args,
             "result": [{"type": "text", "text": "{}"}], "duration_ms": 10},
        ])

        results = list(SessionReplayer(f, dry_run=True).replay())
        assert results[0].arguments == args
        assert results[0].recorded_result == [{"type": "text", "text": "{}"}]


# ---------------------------------------------------------------------------
# MockMCPServer
# ---------------------------------------------------------------------------


class TestMockMCPServer:
    def test_call_returns_matching_recorded_result(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import MockMCPServer

        f = tmp_path / "s.jsonl"
        result_data = [{"type": "text", "text": '{"status": "ok"}'}]
        f.write_text(
            json.dumps({
                "ts": "t", "tool": "agent_status", "arguments": {},
                "result": result_data, "duration_ms": 5,
            }) + "\n"
        )

        mock = MockMCPServer(f)
        result = mock.call("agent_status", {})
        assert result == result_data

    def test_call_returns_none_for_unknown_tool(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import MockMCPServer

        f = tmp_path / "s.jsonl"
        f.write_text(
            json.dumps({
                "ts": "t", "tool": "memory_store", "arguments": {},
                "result": [], "duration_ms": 1,
            }) + "\n"
        )

        mock = MockMCPServer(f)
        result = mock.call("no_such_tool", {})
        assert result is None

    def test_sequential_calls_advance_index(self, tmp_path: Path) -> None:
        from skcapstone.session_replayer import MockMCPServer

        f = tmp_path / "s.jsonl"
        lines = [
            {"ts": "t", "tool": "tool_a", "arguments": {}, "result": [{"t": "a"}], "duration_ms": 1},
            {"ts": "t", "tool": "tool_b", "arguments": {}, "result": [{"t": "b"}], "duration_ms": 2},
        ]
        f.write_text("\n".join(json.dumps(l) for l in lines) + "\n")

        mock = MockMCPServer(f)
        r1 = mock.call("tool_a", {})
        r2 = mock.call("tool_b", {})
        assert r1 == [{"t": "a"}]
        assert r2 == [{"t": "b"}]


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


class TestCLIRecordCommands:
    def test_sessions_list_empty(
        self, tmp_agent_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner
        from skcapstone.cli import main

        monkeypatch.setenv("SKCAPSTONE_ROOT", str(tmp_agent_home.parent))
        runner = CliRunner()
        result = runner.invoke(main, ["sessions", "list", "--home", str(tmp_agent_home)])
        assert result.exit_code == 0
        assert "No sessions" in result.output

    def test_sessions_list_with_sessions(
        self, tmp_agent_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner
        from skcapstone.cli import main
        from skcapstone.session_recorder import SessionRecorder

        rec = SessionRecorder.start_session(tmp_agent_home)
        rec.record("agent_status", {}, [], 5)
        rec.close()

        runner = CliRunner()
        result = runner.invoke(main, ["sessions", "list", "--home", str(tmp_agent_home)])
        assert result.exit_code == 0
        assert "session-" in result.output

    def test_replay_dry_run_cli(
        self, tmp_agent_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json as _json
        from click.testing import CliRunner
        from skcapstone.cli import main

        f = tmp_path / "test_session.jsonl"
        f.write_text(
            _json.dumps({
                "ts": "2026-01-01T00:00:00+00:00",
                "tool": "agent_status",
                "arguments": {},
                "result": [],
                "duration_ms": 3,
            }) + "\n"
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["replay", str(f), "--dry-run", "--home", str(tmp_agent_home)],
        )
        assert result.exit_code == 0
        assert "agent_status" in result.output
        assert "SKIP" in result.output or "dry" in result.output.lower()

    def test_replay_json_format(
        self, tmp_agent_home: Path, tmp_path: Path
    ) -> None:
        import json as _json
        from click.testing import CliRunner
        from skcapstone.cli import main

        f = tmp_path / "test_session.jsonl"
        f.write_text(
            _json.dumps({
                "ts": "2026-01-01T00:00:00+00:00",
                "tool": "coord_status",
                "arguments": {},
                "result": [{"type": "text", "text": "{}"}],
                "duration_ms": 7,
            }) + "\n"
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["replay", str(f), "--dry-run", "--format", "json",
             "--home", str(tmp_agent_home)],
        )
        assert result.exit_code == 0
        rows = _json.loads(result.output)
        assert len(rows) == 1
        assert rows[0]["tool"] == "coord_status"
