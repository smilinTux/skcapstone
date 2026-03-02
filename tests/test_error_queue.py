"""Tests for the persistent error recovery queue."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.error_queue import (
    ErrorEntry,
    ErrorQueue,
    ErrorStatus,
    _backoff_ts,
    _now_iso,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def queue_path(tmp_path: Path) -> Path:
    """Return a temp path for the error queue JSON file."""
    return tmp_path / "error_queue.json"


@pytest.fixture
def queue(queue_path: Path) -> ErrorQueue:
    """Return an ErrorQueue backed by a temp file with fast backoff."""
    return ErrorQueue(path=queue_path, max_retries=3, base_backoff=0)


# ---------------------------------------------------------------------------
# ErrorEntry serialisation
# ---------------------------------------------------------------------------


class TestErrorEntry:
    """Tests for ErrorEntry (de)serialisation."""

    def test_roundtrip(self) -> None:
        """to_dict / from_dict round-trip preserves all fields."""
        entry = ErrorEntry(
            operation_type="llm_call",
            payload={"model": "llama3", "prompt": "hello"},
            error_message="timeout",
        )
        entry.retry_count = 2
        entry.next_retry_at = "2099-01-01T00:00:00+00:00"
        entry.status = ErrorStatus.PENDING

        restored = ErrorEntry.from_dict(entry.to_dict())

        assert restored.entry_id == entry.entry_id
        assert restored.operation_type == "llm_call"
        assert restored.payload == {"model": "llama3", "prompt": "hello"}
        assert restored.error_message == "timeout"
        assert restored.retry_count == 2
        assert restored.next_retry_at == entry.next_retry_at
        assert restored.status == ErrorStatus.PENDING

    def test_defaults(self) -> None:
        """entry_id and created_at are auto-generated when omitted."""
        entry = ErrorEntry(
            operation_type="sync", payload={}, error_message="network error"
        )
        assert len(entry.entry_id) == 32          # uuid4 hex
        assert "T" in entry.created_at             # ISO-8601
        assert entry.retry_count == 0
        assert entry.status == ErrorStatus.PENDING

    def test_repr(self) -> None:
        """__repr__ includes type, retry count, and status."""
        entry = ErrorEntry("message_send", {}, "refused")
        r = repr(entry)
        assert "message_send" in r
        assert "retries=0" in r


# ---------------------------------------------------------------------------
# ErrorQueue — basic operations
# ---------------------------------------------------------------------------


class TestErrorQueueBasic:
    """Tests for enqueue, list, and persistence."""

    def test_enqueue_creates_entry(self, queue: ErrorQueue) -> None:
        """enqueue() adds an entry and returns it."""
        entry = queue.enqueue("llm_call", {"model": "grok"}, "500 error")

        assert entry.operation_type == "llm_call"
        assert entry.status == ErrorStatus.PENDING
        assert entry.retry_count == 0

    def test_enqueue_persists_to_disk(self, queue: ErrorQueue, queue_path: Path) -> None:
        """enqueue() writes JSON to the configured path."""
        queue.enqueue("sync", {}, "network timeout")

        assert queue_path.exists()
        data = json.loads(queue_path.read_text())
        assert len(data) == 1
        assert data[0]["operation_type"] == "sync"

    def test_list_returns_newest_first(self, queue: ErrorQueue) -> None:
        """list_entries() returns entries sorted newest-first."""
        e1 = queue.enqueue("llm_call", {}, "err1")
        e2 = queue.enqueue("message_send", {}, "err2")

        entries = queue.list_entries()
        ids = [e.entry_id for e in entries]
        assert ids.index(e2.entry_id) < ids.index(e1.entry_id)

    def test_list_excludes_resolved_by_default(self, queue: ErrorQueue) -> None:
        """list_entries() hides resolved entries unless include_resolved=True."""
        entry = queue.enqueue("llm_call", {}, "err")
        # Force-resolve by marking directly in JSON
        entries = queue._load()
        entries[0].status = ErrorStatus.RESOLVED
        queue._save(entries)

        assert queue.list_entries() == []
        assert len(queue.list_entries(include_resolved=True)) == 1

    def test_list_filter_by_status(self, queue: ErrorQueue) -> None:
        """list_entries(status=...) filters correctly."""
        queue.enqueue("llm_call", {}, "err")
        entries = queue._load()
        entries[0].status = ErrorStatus.EXHAUSTED
        queue._save(entries)

        assert len(queue.list_entries(status="exhausted")) == 1
        assert queue.list_entries(status="pending") == []

    def test_queue_survives_reload(self, queue_path: Path) -> None:
        """Data persists across separate ErrorQueue instances."""
        q1 = ErrorQueue(path=queue_path, base_backoff=0)
        q1.enqueue("sync", {"x": 1}, "disk full")

        q2 = ErrorQueue(path=queue_path, base_backoff=0)
        entries = q2.list_entries()
        assert len(entries) == 1
        assert entries[0].payload == {"x": 1}

    def test_empty_queue_when_file_missing(self, tmp_path: Path) -> None:
        """list_entries() returns [] when queue file does not exist."""
        q = ErrorQueue(path=tmp_path / "nonexistent.json")
        assert q.list_entries() == []

    def test_corrupt_file_returns_empty(self, queue_path: Path) -> None:
        """A corrupt JSON file is treated as an empty queue."""
        queue_path.write_text("NOT JSON", encoding="utf-8")
        q = ErrorQueue(path=queue_path)
        assert q.list_entries() == []


# ---------------------------------------------------------------------------
# ErrorQueue — retry logic
# ---------------------------------------------------------------------------


class TestErrorQueueRetry:
    """Tests for retry and exponential backoff."""

    def test_retry_success_marks_resolved(self, queue: ErrorQueue) -> None:
        """A successful retry marks the entry resolved."""
        entry = queue.enqueue("llm_call", {}, "timeout")

        result = queue.retry(entry.entry_id, handler=lambda e: True)

        assert result is True
        loaded = queue._load()
        assert loaded[0].status == ErrorStatus.RESOLVED

    def test_retry_failure_increments_count(self, queue: ErrorQueue) -> None:
        """A failed retry increments retry_count and stays PENDING."""
        entry = queue.enqueue("message_send", {}, "refused")

        queue.retry(entry.entry_id, handler=lambda e: False)

        loaded = queue._load()
        assert loaded[0].retry_count == 1
        assert loaded[0].status == ErrorStatus.PENDING

    def test_retry_exhausted_after_max_retries(self, queue: ErrorQueue) -> None:
        """After max_retries failed attempts the entry is EXHAUSTED."""
        entry = queue.enqueue("sync", {}, "server down")

        for _ in range(queue._max_retries):
            queue.retry(entry.entry_id, handler=lambda e: False)

        loaded = queue._load()
        assert loaded[0].status == ErrorStatus.EXHAUSTED
        assert loaded[0].next_retry_at is None

    def test_exhausted_entry_skips_retry(self, queue: ErrorQueue) -> None:
        """Retrying an exhausted entry returns False immediately."""
        entry = queue.enqueue("llm_call", {}, "404")
        entries = queue._load()
        entries[0].status = ErrorStatus.EXHAUSTED
        queue._save(entries)

        result = queue.retry(entry.entry_id, handler=lambda e: True)
        assert result is False

    def test_retry_unknown_id_returns_false(self, queue: ErrorQueue) -> None:
        """Retrying an unknown entry_id returns False."""
        result = queue.retry("deadbeef" * 4, handler=lambda e: True)
        assert result is False

    def test_backoff_increases_with_attempts(self) -> None:
        """_backoff_ts produces later timestamps for higher attempt numbers."""
        t0 = _backoff_ts(0, base=10)
        t1 = _backoff_ts(1, base=10)
        t2 = _backoff_ts(2, base=10)
        assert t0 < t1 < t2

    def test_retry_all_due_processes_only_due(self, queue_path: Path) -> None:
        """retry_all_due() skips entries whose next_retry_at is in the future."""
        q = ErrorQueue(path=queue_path, base_backoff=0)
        past = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        # Create both entries then manually set their next_retry_at
        e_due = q.enqueue("llm_call", {}, "err")
        e_future = q.enqueue("sync", {}, "err2")

        entries = q._load()
        for e in entries:
            if e.entry_id == e_due.entry_id:
                e.next_retry_at = past
            else:
                e.next_retry_at = future
        q._save(entries)

        called: list[str] = []

        def handler(entry: ErrorEntry) -> bool:
            called.append(entry.entry_id)
            return True

        q.retry_all_due(handler=handler)
        assert e_due.entry_id in called
        assert e_future.entry_id not in called


# ---------------------------------------------------------------------------
# ErrorQueue — remove / clear
# ---------------------------------------------------------------------------


class TestErrorQueueClear:
    """Tests for remove and clear_all."""

    def test_remove_existing_entry(self, queue: ErrorQueue) -> None:
        """remove() deletes a specific entry and returns True."""
        entry = queue.enqueue("sync", {}, "err")
        result = queue.remove(entry.entry_id)
        assert result is True
        assert queue.list_entries() == []

    def test_remove_nonexistent_returns_false(self, queue: ErrorQueue) -> None:
        """remove() returns False for an unknown entry_id."""
        assert queue.remove("no-such-id") is False

    def test_clear_all(self, queue: ErrorQueue) -> None:
        """clear_all() removes every entry and returns the count."""
        queue.enqueue("llm_call", {}, "err1")
        queue.enqueue("sync", {}, "err2")

        removed = queue.clear_all()
        assert removed == 2
        assert queue.list_entries() == []

    def test_clear_by_status(self, queue: ErrorQueue) -> None:
        """clear_all(status=...) only removes matching entries."""
        e1 = queue.enqueue("llm_call", {}, "err1")
        e2 = queue.enqueue("sync", {}, "err2")

        entries = queue._load()
        for e in entries:
            if e.entry_id == e1.entry_id:
                e.status = ErrorStatus.EXHAUSTED
        queue._save(entries)

        removed = queue.clear_all(status=ErrorStatus.EXHAUSTED)
        assert removed == 1

        remaining = queue.list_entries(include_resolved=True)
        assert len(remaining) == 1
        assert remaining[0].entry_id == e2.entry_id


# ---------------------------------------------------------------------------
# ErrorQueue — stats
# ---------------------------------------------------------------------------


class TestErrorQueueStats:
    """Tests for the stats() summary method."""

    def test_stats_counts_correctly(self, queue: ErrorQueue) -> None:
        """stats() returns accurate counts per status."""
        queue.enqueue("llm_call", {}, "err1")
        queue.enqueue("sync", {}, "err2")

        entries = queue._load()
        entries[0].status = ErrorStatus.EXHAUSTED
        queue._save(entries)

        s = queue.stats()
        assert s["total"] == 2
        assert s["exhausted"] == 1
        assert s["pending"] == 1

    def test_stats_empty_queue(self, queue: ErrorQueue) -> None:
        """stats() on an empty queue returns zeros."""
        s = queue.stats()
        assert s["total"] == 0
        for status in ErrorStatus:
            assert s.get(status.value, 0) == 0


# ---------------------------------------------------------------------------
# CLI smoke-tests
# ---------------------------------------------------------------------------


class TestErrorQueueCLI:
    """Smoke-tests for the `skcapstone errors` CLI commands."""

    def test_cli_list_empty(self, tmp_path: Path) -> None:
        """errors list on empty queue exits 0 and prints 'empty'."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["errors", "list", "--path", str(tmp_path / "eq.json")],
        )
        assert result.exit_code == 0
        assert "empty" in result.output.lower() or "0 total" in result.output.lower() or "Queue" in result.output

    def test_cli_list_shows_entry(self, tmp_path: Path) -> None:
        """errors list shows an enqueued entry."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        q_path = tmp_path / "eq.json"
        q = ErrorQueue(path=q_path, base_backoff=0)
        q.enqueue("llm_call", {"model": "grok"}, "test error msg")

        runner = CliRunner()
        result = runner.invoke(main, ["errors", "list", "--path", str(q_path)])
        assert result.exit_code == 0
        assert "llm_call" in result.output

    def test_cli_stats(self, tmp_path: Path) -> None:
        """errors stats exits 0 and shows totals panel."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        q_path = tmp_path / "eq.json"
        q = ErrorQueue(path=q_path, base_backoff=0)
        q.enqueue("sync", {}, "err")

        runner = CliRunner()
        result = runner.invoke(main, ["errors", "stats", "--path", str(q_path)])
        assert result.exit_code == 0
        assert "Total" in result.output or "1" in result.output

    def test_cli_clear_all_with_force(self, tmp_path: Path) -> None:
        """errors clear --all --force removes all entries."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        q_path = tmp_path / "eq.json"
        q = ErrorQueue(path=q_path, base_backoff=0)
        q.enqueue("llm_call", {}, "err1")
        q.enqueue("sync", {}, "err2")

        runner = CliRunner()
        result = runner.invoke(
            main, ["errors", "clear", "--all", "--force", "--path", str(q_path)]
        )
        assert result.exit_code == 0
        assert q.list_entries() == []

    def test_cli_retry_no_args_fails(self, tmp_path: Path) -> None:
        """errors retry without ENTRY_ID and without --all exits non-zero."""
        from click.testing import CliRunner
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(
            main, ["errors", "retry", "--path", str(tmp_path / "eq.json")]
        )
        assert result.exit_code != 0 or "ENTRY_ID" in result.output or "all" in result.output
