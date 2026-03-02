"""Tests for ConversationArchiver — conversation archival and compression."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from skcapstone.archiver import (
    ConversationArchiver,
    ArchiveResult,
    ArchiveSummary,
    _parse_ts,
    _load_messages,
    _save_messages,
    _load_archive,
    _save_archive,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(days_ago: float) -> str:
    """Return an ISO-8601 UTC timestamp for N days ago."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.isoformat()


def _make_msg(role: str, content: str, days_ago: float) -> dict:
    return {"role": role, "content": content, "timestamp": _ts(days_ago)}


def _write_conv(conv_dir: Path, peer: str, messages: list[dict]) -> Path:
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = conv_dir / f"{peer}.json"
    path.write_text(json.dumps(messages), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / ".skcapstone"
    h.mkdir()
    return h


@pytest.fixture
def archiver(home) -> ConversationArchiver:
    return ConversationArchiver(home, age_days=30, keep_recent=100)


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------


class TestParseTs:
    def test_valid_utc_string(self):
        ts = "2026-01-01T10:00:00+00:00"
        dt = _parse_ts(ts)
        assert dt is not None
        assert dt.tzinfo is not None

    def test_naive_datetime_gets_utc(self):
        dt = _parse_ts("2026-01-01T10:00:00")
        assert dt.tzinfo == timezone.utc

    def test_none_returns_none(self):
        assert _parse_ts(None) is None

    def test_invalid_string_returns_none(self):
        assert _parse_ts("not-a-date") is None

    def test_empty_string_returns_none(self):
        assert _parse_ts("") is None


# ---------------------------------------------------------------------------
# _load_messages / _save_messages
# ---------------------------------------------------------------------------


class TestLoadSaveMessages:
    def test_round_trip(self, tmp_path):
        msgs = [{"role": "user", "content": "hello", "timestamp": _ts(1)}]
        path = tmp_path / "peer.json"
        _save_messages(path, msgs)
        loaded = _load_messages(path)
        assert loaded == msgs

    def test_load_missing_file_returns_empty(self, tmp_path):
        assert _load_messages(tmp_path / "nonexistent.json") == []

    def test_load_corrupt_file_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        assert _load_messages(path) == []

    def test_load_non_list_json_returns_empty(self, tmp_path):
        path = tmp_path / "dict.json"
        path.write_text(json.dumps({"not": "a list"}))
        assert _load_messages(path) == []

    def test_save_atomic_no_tmp_file(self, tmp_path):
        path = tmp_path / "peer.json"
        _save_messages(path, [])
        assert not (tmp_path / "peer.json.tmp").exists()


# ---------------------------------------------------------------------------
# _load_archive / _save_archive
# ---------------------------------------------------------------------------


class TestLoadSaveArchive:
    def test_round_trip_gzip(self, tmp_path):
        msgs = [{"role": "user", "content": "archived", "timestamp": _ts(60)}]
        path = tmp_path / "peer.json.gz"
        _save_archive(path, msgs)
        loaded = _load_archive(path)
        assert len(loaded) == 1
        assert loaded[0]["content"] == "archived"

    def test_is_actually_compressed(self, tmp_path):
        path = tmp_path / "peer.json.gz"
        _save_archive(path, [{"role": "user", "content": "x", "timestamp": _ts(60)}])
        # Verify the file is gzip magic bytes (1f 8b)
        raw = path.read_bytes()
        assert raw[:2] == b"\x1f\x8b"

    def test_missing_file_returns_empty(self, tmp_path):
        assert _load_archive(tmp_path / "missing.json.gz") == []

    def test_corrupt_archive_returns_empty(self, tmp_path):
        path = tmp_path / "bad.json.gz"
        path.write_bytes(b"not gzip data")
        assert _load_archive(path) == []

    def test_sorted_by_timestamp(self, tmp_path):
        msgs = [
            {"role": "user", "content": "newer", "timestamp": _ts(30)},
            {"role": "user", "content": "older", "timestamp": _ts(90)},
        ]
        path = tmp_path / "peer.json.gz"
        _save_archive(path, msgs)
        loaded = _load_archive(path)
        assert loaded[0]["content"] == "older"
        assert loaded[1]["content"] == "newer"

    def test_creates_parent_dirs(self, tmp_path):
        deep = tmp_path / "a" / "b" / "peer.json.gz"
        _save_archive(deep, [])
        assert deep.exists()


# ---------------------------------------------------------------------------
# ConversationArchiver._partition
# ---------------------------------------------------------------------------


class TestPartition:
    def test_all_recent_kept(self, archiver):
        msgs = [_make_msg("user", f"msg {i}", i) for i in range(5)]
        retain, to_archive = archiver._partition(msgs)
        assert len(retain) == 5
        assert len(to_archive) == 0

    def test_old_messages_beyond_keep_recent_archived(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=2)
        msgs = [
            _make_msg("user", "ancient", 90),
            _make_msg("user", "old", 60),
            _make_msg("user", "recent1", 5),
            _make_msg("user", "recent2", 1),
        ]
        retain, to_archive = arch._partition(msgs)
        # keep_recent=2 → last 2 always retained; first 2 are old → archived
        assert len(retain) == 2
        assert len(to_archive) == 2
        contents = {m["content"] for m in to_archive}
        assert "ancient" in contents
        assert "old" in contents

    def test_old_messages_within_keep_recent_not_archived(self, home):
        # keep_recent=100 means all 5 messages are protected even if old
        arch = ConversationArchiver(home, age_days=30, keep_recent=100)
        msgs = [_make_msg("user", f"msg {i}", 60) for i in range(5)]
        retain, to_archive = arch._partition(msgs)
        assert len(to_archive) == 0
        assert len(retain) == 5

    def test_message_without_timestamp_always_retained(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        msgs = [
            {"role": "user", "content": "no-ts", "timestamp": None},
            _make_msg("user", "protected", 1),
        ]
        retain, to_archive = arch._partition(msgs)
        # The no-ts message is not in the last 1, but has no parseable ts → retained
        assert all(m["content"] != "no-ts" or m in retain for m in msgs)
        assert len(to_archive) == 0

    def test_empty_messages_returns_empty_lists(self, archiver):
        assert archiver._partition([]) == ([], [])


# ---------------------------------------------------------------------------
# ConversationArchiver.archive_peer
# ---------------------------------------------------------------------------


class TestArchivePeer:
    def test_missing_peer_file_returns_skipped(self, archiver):
        result = archiver.archive_peer("nobody")
        assert result.skipped is True
        assert result.peer == "nobody"

    def test_no_archivable_messages_returns_skipped(self, home, archiver):
        conv_dir = home / "conversations"
        msgs = [_make_msg("user", "fresh", 1)]
        _write_conv(conv_dir, "jarvis", msgs)

        result = archiver.archive_peer("jarvis")
        assert result.skipped is True

    def test_archives_old_messages(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        msgs = [
            _make_msg("user", "ancient", 90),
            _make_msg("user", "recent", 1),
        ]
        _write_conv(conv_dir, "alice", msgs)

        result = arch.archive_peer("alice")
        assert result.archived_count == 1
        assert result.retained_count == 1
        assert result.skipped is False

    def test_archive_file_is_gzip_compressed(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        msgs = [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ]
        _write_conv(conv_dir, "bob", msgs)
        result = arch.archive_peer("bob")

        assert result.archive_path is not None
        raw = result.archive_path.read_bytes()
        assert raw[:2] == b"\x1f\x8b", "Archive file must be gzip-compressed"

    def test_active_file_rewritten_with_retained_only(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        msgs = [
            _make_msg("user", "ancient", 90),
            _make_msg("user", "recent", 2),
        ]
        _write_conv(conv_dir, "carol", msgs)
        arch.archive_peer("carol")

        active = json.loads((conv_dir / "carol.json").read_text())
        assert len(active) == 1
        assert active[0]["content"] == "recent"

    def test_archive_accumulates_across_runs(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"

        # First run
        _write_conv(conv_dir, "dave", [
            _make_msg("user", "very old", 120),
            _make_msg("user", "latest", 2),
        ])
        arch.archive_peer("dave")

        # Second run: add another old message
        _write_conv(conv_dir, "dave", [
            _make_msg("user", "also old", 90),
            _make_msg("user", "newest", 1),
        ])
        arch.archive_peer("dave")

        archive_path = home / "archive" / "dave.json.gz"
        archived = _load_archive(archive_path)
        contents = {m["content"] for m in archived}
        assert "very old" in contents
        assert "also old" in contents

    def test_archive_placed_in_archive_dir(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        _write_conv(conv_dir, "eve", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])
        result = arch.archive_peer("eve")
        assert result.archive_path == home / "archive" / "eve.json.gz"


# ---------------------------------------------------------------------------
# ConversationArchiver.archive_all
# ---------------------------------------------------------------------------


class TestArchiveAll:
    def test_empty_conversations_dir_returns_empty_summary(self, archiver):
        summary = archiver.archive_all()
        assert summary.peers_processed == 0
        assert summary.total_archived == 0

    def test_archives_all_peers(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        for peer in ("alice", "bob", "carol"):
            _write_conv(conv_dir, peer, [
                _make_msg("user", "old", 60),
                _make_msg("user", "new", 1),
            ])

        summary = arch.archive_all()
        assert summary.peers_processed == 3
        assert summary.total_archived == 3
        assert summary.total_retained == 3

    def test_skips_peers_with_nothing_to_archive(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=100)
        conv_dir = home / "conversations"
        _write_conv(conv_dir, "fresh-peer", [_make_msg("user", "recent", 1)])

        summary = arch.archive_all()
        assert summary.peers_skipped == 1
        assert summary.total_archived == 0

    def test_summary_results_list_populated(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        _write_conv(conv_dir, "zara", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])
        summary = arch.archive_all()
        assert len(summary.results) == 1
        assert summary.results[0].peer == "zara"


# ---------------------------------------------------------------------------
# ConversationArchiver.list_archives
# ---------------------------------------------------------------------------


class TestListArchives:
    def test_empty_archive_dir_returns_empty(self, archiver):
        assert archiver.list_archives() == []

    def test_lists_existing_archives(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        _write_conv(conv_dir, "listme", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])
        arch.archive_peer("listme")

        archives = arch.list_archives()
        assert len(archives) == 1
        assert archives[0]["peer"] == "listme"
        assert archives[0]["message_count"] == 1
        assert archives[0]["size_bytes"] > 0

    def test_list_includes_path_key(self, home):
        arch = ConversationArchiver(home, age_days=30, keep_recent=1)
        conv_dir = home / "conversations"
        _write_conv(conv_dir, "pathtest", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])
        arch.archive_peer("pathtest")

        archives = arch.list_archives()
        assert "path" in archives[0]
        assert archives[0]["path"].exists()


# ---------------------------------------------------------------------------
# Custom archive directory
# ---------------------------------------------------------------------------


class TestCustomArchiveDir:
    def test_custom_archive_dir_used(self, tmp_path):
        home = tmp_path / ".skcapstone"
        home.mkdir()
        custom_dir = tmp_path / "my_archives"
        arch = ConversationArchiver(home, age_days=30, keep_recent=1, archive_dir=custom_dir)

        conv_dir = home / "conversations"
        _write_conv(conv_dir, "custom", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])
        result = arch.archive_peer("custom")

        assert result.archive_path.parent == custom_dir


# ---------------------------------------------------------------------------
# CLI integration (smoke tests via Click test runner)
# ---------------------------------------------------------------------------


class TestArchiveCLI:
    def test_archive_run_nothing_to_archive(self, home):
        from click.testing import CliRunner
        from skcapstone.cli import main

        conv_dir = home / "conversations"
        _write_conv(conv_dir, "peer", [_make_msg("user", "fresh", 1)])

        runner = CliRunner()
        result = runner.invoke(main, ["archive", "run", "--home", str(home)])
        assert result.exit_code == 0
        assert "Nothing to archive" in result.output

    def test_archive_run_with_archivable_messages(self, home):
        from click.testing import CliRunner
        from skcapstone.cli import main

        conv_dir = home / "conversations"
        _write_conv(conv_dir, "oldpeer", [
            _make_msg("user", "old message", 60),
            _make_msg("user", "new message", 1),
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["archive", "run", "--home", str(home), "--keep-recent", "1"],
        )
        assert result.exit_code == 0

    def test_archive_list_no_archives(self, home):
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["archive", "list", "--home", str(home)])
        assert result.exit_code == 0
        assert "No archives" in result.output

    def test_archive_dry_run_shows_preview(self, home):
        from click.testing import CliRunner
        from skcapstone.cli import main

        conv_dir = home / "conversations"
        _write_conv(conv_dir, "drypeer", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["archive", "run", "--home", str(home), "--keep-recent", "1", "--dry-run"],
        )
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
        # Dry run must not actually create any archive file
        assert not (home / "archive" / "drypeer.json.gz").exists()

    def test_archive_list_after_run(self, home):
        from click.testing import CliRunner
        from skcapstone.cli import main

        conv_dir = home / "conversations"
        _write_conv(conv_dir, "listpeer", [
            _make_msg("user", "old", 60),
            _make_msg("user", "new", 1),
        ])

        runner = CliRunner()
        runner.invoke(main, ["archive", "run", "--home", str(home), "--keep-recent", "1"])
        result = runner.invoke(main, ["archive", "list", "--home", str(home)])
        assert result.exit_code == 0
        assert "listpeer" in result.output
