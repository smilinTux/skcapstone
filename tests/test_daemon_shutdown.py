"""Tests for graceful shutdown with state persistence in DaemonService.

Covers:
- _save_shutdown_state writes correct JSON with inflight + metrics + timestamp
- _load_startup_state restores metrics and removes the state file
- _load_startup_state is a no-op when no state file exists
- _resume_inflight_messages replays messages via consciousness loop
- _resume_inflight_messages drops messages gracefully when consciousness unavailable
- DaemonState inflight tracking (add / remove / get)
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.daemon import (
    PID_FILE,
    SHUTDOWN_STATE_FILE,
    DaemonConfig,
    DaemonService,
    DaemonState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path: Path) -> DaemonService:
    """Create a DaemonService backed by a temp directory (no real threads)."""
    config = DaemonConfig(
        home=tmp_path,
        shared_root=tmp_path / "shared",
        poll_interval=10,
        sync_interval=300,
        consciousness_enabled=False,
    )
    return DaemonService(config)


def _make_fake_envelope(sender: str = "peer-a", content: str = "hello") -> SimpleNamespace:
    """Build a minimal fake SKComms envelope."""
    return SimpleNamespace(
        message_id=str(uuid.uuid4()),
        sender=sender,
        payload=SimpleNamespace(
            content=content,
            content_type=SimpleNamespace(value="text"),
        ),
    )


# ---------------------------------------------------------------------------
# DaemonState - inflight tracking
# ---------------------------------------------------------------------------


class TestDaemonStateInflight:
    """Unit tests for add/remove/get inflight on DaemonState."""

    def test_add_inflight_stores_data(self) -> None:
        """add_inflight stores the message data under its ID."""
        state = DaemonState()
        state.add_inflight("msg-1", {"sender": "alice", "content": "hi"})
        assert len(state.get_inflight()) == 1
        assert state.get_inflight()[0]["sender"] == "alice"

    def test_remove_inflight_clears_entry(self) -> None:
        """remove_inflight removes the entry; subsequent get returns empty list."""
        state = DaemonState()
        state.add_inflight("msg-1", {"sender": "alice"})
        state.remove_inflight("msg-1")
        assert state.get_inflight() == []

    def test_remove_inflight_unknown_id_is_noop(self) -> None:
        """remove_inflight with an unknown ID does not raise."""
        state = DaemonState()
        state.remove_inflight("nonexistent")  # should not raise

    def test_get_inflight_returns_snapshot(self) -> None:
        """get_inflight returns a list copy - mutations don't affect stored state."""
        state = DaemonState()
        state.add_inflight("a", {"x": 1})
        state.add_inflight("b", {"x": 2})
        result = state.get_inflight()
        assert len(result) == 2
        result.clear()  # mutate the returned list
        assert len(state.get_inflight()) == 2  # stored state unchanged

    def test_snapshot_includes_inflight_count(self) -> None:
        """DaemonState.snapshot() includes inflight_count."""
        state = DaemonState()
        state.add_inflight("m1", {"content": "test"})
        snap = state.snapshot()
        assert snap["inflight_count"] == 1


# ---------------------------------------------------------------------------
# _save_shutdown_state
# ---------------------------------------------------------------------------


class TestSaveShutdownState:
    """Tests for _save_shutdown_state."""

    def test_writes_json_file(self, tmp_path: Path) -> None:
        """Shutdown state file is created with correct structure."""
        svc = _make_service(tmp_path)
        svc.state.messages_received = 7
        svc.state.syncs_completed = 2
        svc.state.add_inflight("m1", {"message_id": "m1", "sender": "bob", "content": "hey"})

        svc._save_shutdown_state()

        state_file = tmp_path / SHUTDOWN_STATE_FILE
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["metrics"]["messages_received"] == 7
        assert data["metrics"]["syncs_completed"] == 2
        assert len(data["inflight_messages"]) == 1
        assert data["inflight_messages"][0]["sender"] == "bob"
        assert "shutdown_at" in data

    def test_writes_empty_inflight_when_no_messages_pending(self, tmp_path: Path) -> None:
        """Shutdown state is still written when no messages are in flight."""
        svc = _make_service(tmp_path)
        svc._save_shutdown_state()

        data = json.loads((tmp_path / SHUTDOWN_STATE_FILE).read_text())
        assert data["inflight_messages"] == []

    def test_handles_write_error_gracefully(self, tmp_path: Path) -> None:
        """_save_shutdown_state logs an error and does not raise on write failure."""
        svc = _make_service(tmp_path)
        # Point home at a path whose parent doesn't exist so write fails
        svc.config.home = tmp_path / "no_such_dir" / "nope"  # type: ignore[assignment]
        svc._save_shutdown_state()  # should log error, not raise


# ---------------------------------------------------------------------------
# _load_startup_state
# ---------------------------------------------------------------------------


class TestLoadStartupState:
    """Tests for _load_startup_state."""

    def test_no_op_when_file_missing(self, tmp_path: Path) -> None:
        """_load_startup_state does nothing when no shutdown state file exists."""
        svc = _make_service(tmp_path)
        svc._load_startup_state()  # should not raise
        assert svc.state.messages_received == 0

    def test_restores_metrics_from_file(self, tmp_path: Path) -> None:
        """metrics are accumulated from the state file into current state."""
        state_file = tmp_path / SHUTDOWN_STATE_FILE
        state_file.write_text(
            json.dumps(
                {
                    "shutdown_at": "2026-03-01T00:00:00+00:00",
                    "inflight_messages": [],
                    "metrics": {"messages_received": 42, "syncs_completed": 5},
                }
            ),
            encoding="utf-8",
        )

        svc = _make_service(tmp_path)
        svc._load_startup_state()

        assert svc.state.messages_received == 42
        assert svc.state.syncs_completed == 5

    def test_removes_state_file_after_load(self, tmp_path: Path) -> None:
        """The shutdown_state.json is deleted after successful load."""
        state_file = tmp_path / SHUTDOWN_STATE_FILE
        state_file.write_text(
            json.dumps(
                {
                    "shutdown_at": "2026-03-01T00:00:00+00:00",
                    "inflight_messages": [],
                    "metrics": {},
                }
            ),
            encoding="utf-8",
        )

        svc = _make_service(tmp_path)
        svc._load_startup_state()

        assert not state_file.exists()

    def test_corrupted_file_does_not_raise(self, tmp_path: Path) -> None:
        """Corrupt JSON in shutdown_state.json is handled gracefully."""
        (tmp_path / SHUTDOWN_STATE_FILE).write_text("not json{{{", encoding="utf-8")
        svc = _make_service(tmp_path)
        svc._load_startup_state()  # should log warning and return
        assert svc.state.messages_received == 0


# ---------------------------------------------------------------------------
# _resume_inflight_messages
# ---------------------------------------------------------------------------


class TestResumeInflightMessages:
    """Tests for _resume_inflight_messages."""

    def test_drops_messages_when_consciousness_unavailable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Messages are logged as dropped when consciousness is not loaded."""
        svc = _make_service(tmp_path)
        svc._consciousness = None

        inflight = [
            {"message_id": "x1", "sender": "alice", "content": "hi", "content_type": "text"},
        ]
        svc._resume_inflight_messages(inflight)  # should not raise

    def test_replays_messages_through_consciousness(self, tmp_path: Path) -> None:
        """In-flight messages are replayed via consciousness.process_envelope."""
        svc = _make_service(tmp_path)

        mock_consciousness = MagicMock()
        mock_consciousness._config.enabled = True
        svc._consciousness = mock_consciousness

        inflight = [
            {"message_id": "m1", "sender": "bob", "content": "test msg", "content_type": "text"},
            {"message_id": "m2", "sender": "carol", "content": "another", "content_type": "text"},
        ]
        svc._resume_inflight_messages(inflight)

        assert mock_consciousness.process_envelope.call_count == 2
        calls = mock_consciousness.process_envelope.call_args_list
        senders = {c.args[0].sender for c in calls}
        assert senders == {"bob", "carol"}

    def test_replayed_envelope_has_correct_content(self, tmp_path: Path) -> None:
        """The reconstructed envelope carries the original content and sender."""
        svc = _make_service(tmp_path)
        captured: list = []

        mock_consciousness = MagicMock()
        mock_consciousness._config.enabled = True
        mock_consciousness.process_envelope.side_effect = lambda env: captured.append(env)
        svc._consciousness = mock_consciousness

        inflight = [
            {"message_id": "q1", "sender": "dave", "content": "resume me", "content_type": "text"},
        ]
        svc._resume_inflight_messages(inflight)

        assert len(captured) == 1
        env = captured[0]
        assert env.sender == "dave"
        assert env.payload.content == "resume me"
        assert env.payload.content_type.value == "text"

    def test_resume_error_on_one_message_continues_others(self, tmp_path: Path) -> None:
        """A failure on one message does not prevent the rest from being resumed."""
        svc = _make_service(tmp_path)
        processed: list = []

        def side_effect(env):
            if env.sender == "bad":
                raise RuntimeError("boom")
            processed.append(env.sender)

        mock_consciousness = MagicMock()
        mock_consciousness._config.enabled = True
        mock_consciousness.process_envelope.side_effect = side_effect
        svc._consciousness = mock_consciousness

        inflight = [
            {"message_id": "a", "sender": "bad", "content": "x", "content_type": "text"},
            {"message_id": "b", "sender": "good", "content": "y", "content_type": "text"},
        ]
        svc._resume_inflight_messages(inflight)

        assert "good" in processed


# ---------------------------------------------------------------------------
# Integration: save → load round-trip
# ---------------------------------------------------------------------------


class TestShutdownStartupRoundTrip:
    """End-to-end: save state then load it back."""

    def test_round_trip_preserves_inflight_content(self, tmp_path: Path) -> None:
        """State saved on shutdown is accurately restored on the next start."""
        svc = _make_service(tmp_path)
        svc.state.messages_received = 10
        svc.state.syncs_completed = 3
        svc.state.add_inflight(
            "rt1",
            {
                "message_id": "rt1",
                "sender": "peer-x",
                "content": "round trip payload",
                "content_type": "text",
                "received_at": "2026-03-02T00:00:00+00:00",
            },
        )

        svc._save_shutdown_state()

        # Simulate a fresh service startup
        svc2 = _make_service(tmp_path)
        resumed: list = []

        mock_consciousness = MagicMock()
        mock_consciousness._config.enabled = True
        mock_consciousness.process_envelope.side_effect = lambda env: resumed.append(env)
        svc2._consciousness = mock_consciousness

        svc2._load_startup_state()

        assert svc2.state.messages_received == 10
        assert svc2.state.syncs_completed == 3
        assert len(resumed) == 1
        assert resumed[0].payload.content == "round trip payload"
        assert resumed[0].sender == "peer-x"
        assert not (tmp_path / SHUTDOWN_STATE_FILE).exists()


# ---------------------------------------------------------------------------
# Graceful stop() - pidfile removal, component teardown, bounded, idempotent
# ---------------------------------------------------------------------------


class TestGracefulStop:
    """Tests for the bounded, idempotent stop() shutdown sequence."""

    def test_stop_removes_pidfile(self, tmp_path: Path) -> None:
        """A stale PID file is removed by stop() so a restart never sees one."""
        svc = _make_service(tmp_path)
        svc._write_pid()
        assert (tmp_path / PID_FILE).exists()

        svc.stop()

        assert not (tmp_path / PID_FILE).exists()

    def test_stop_sets_stop_event_and_clears_running(self, tmp_path: Path) -> None:
        """stop() signals every loop to quit and marks the daemon not-running."""
        svc = _make_service(tmp_path)
        svc.state.running = True

        svc.stop()

        assert svc._stop_event.is_set()
        assert svc.state.running is False

    def test_stop_stops_consciousness_and_scheduler(self, tmp_path: Path) -> None:
        """Registered components (consciousness + scheduler) are stopped cleanly."""
        svc = _make_service(tmp_path)
        mock_consciousness = MagicMock()
        mock_scheduler = MagicMock()
        svc._consciousness = mock_consciousness
        svc._scheduler = mock_scheduler

        svc.stop()

        mock_consciousness.stop.assert_called_once()
        mock_scheduler.stop.assert_called_once()

    def test_stop_publishes_offline_heartbeat(self, tmp_path: Path) -> None:
        """stop() publishes a final offline heartbeat via the beacon."""
        svc = _make_service(tmp_path)
        mock_beacon = MagicMock()
        svc._beacon = mock_beacon

        svc.stop()

        mock_beacon.mark_offline.assert_called_once()

    def test_stop_writes_shutdown_state(self, tmp_path: Path) -> None:
        """stop() flushes in-flight state to disk (retry-queue drain)."""
        svc = _make_service(tmp_path)
        svc.state.add_inflight("m1", {"message_id": "m1", "sender": "z", "content": "c"})

        svc.stop()

        assert (tmp_path / SHUTDOWN_STATE_FILE).exists()

    def test_stop_writes_peaceful_journal_entry(self, tmp_path: Path) -> None:
        """stop() writes a 'Daemon shutdown' journal entry with a peaceful tone."""
        svc = _make_service(tmp_path)
        written: list = []

        fake_journal = MagicMock()
        fake_journal.write_entry.side_effect = lambda e: written.append(e)
        fake_module = SimpleNamespace(
            Journal=MagicMock(return_value=fake_journal),
            JournalEntry=lambda **kw: SimpleNamespace(**kw),
        )
        with patch.dict("sys.modules", {"skmemory.journal": fake_module}):
            svc.stop()

        assert len(written) == 1
        assert written[0].title == "Daemon shutdown"
        assert written[0].emotional_summary == "peaceful"

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        """A second stop() call is a no-op - components stopped only once."""
        svc = _make_service(tmp_path)
        mock_consciousness = MagicMock()
        svc._consciousness = mock_consciousness

        svc.stop()
        svc.stop()  # duplicate signal / run_forever finally

        mock_consciousness.stop.assert_called_once()

    def test_double_signal_is_safe(self, tmp_path: Path) -> None:
        """Repeated SIGTERM/SIGINT only ever set the stop event; body runs once."""
        svc = _make_service(tmp_path)
        mock_beacon = MagicMock()
        svc._beacon = mock_beacon

        # Simulate the signal handler firing twice, then the actual stop.
        svc._handle_signal(15, None)
        svc._handle_signal(2, None)
        assert svc._stop_event.is_set()

        svc.stop()
        svc.stop()

        mock_beacon.mark_offline.assert_called_once()

    def test_stop_bounded_when_thread_hangs(self, tmp_path: Path) -> None:
        """A wedged worker thread cannot make stop() exceed its time budget."""
        svc = _make_service(tmp_path)
        svc.config.shutdown_timeout = 0.5

        never = threading.Event()  # never set - thread ignores the stop signal
        hung = threading.Thread(target=never.wait, name="wedged-worker", daemon=True)
        hung.start()
        svc._threads = [hung]

        start = time.monotonic()
        svc.stop()
        elapsed = time.monotonic() - start

        # Bounded: must return in roughly the budget, not block on the hung join.
        assert elapsed < 5.0
        # PID cleanup still happens despite the wedged thread.
        assert not (tmp_path / PID_FILE).exists()
        never.set()

    def test_stop_survives_component_error(self, tmp_path: Path) -> None:
        """An exception from a component still leaves the PID file removed."""
        svc = _make_service(tmp_path)
        svc._write_pid()
        mock_consciousness = MagicMock()
        mock_consciousness.stop.side_effect = RuntimeError("boom")
        svc._consciousness = mock_consciousness

        svc.stop()  # must not raise

        assert not (tmp_path / PID_FILE).exists()

    def test_run_forever_calls_stop_once(self, tmp_path: Path) -> None:
        """run_forever's finally triggers exactly one shutdown even after a signal."""
        svc = _make_service(tmp_path)
        mock_beacon = MagicMock()
        svc._beacon = mock_beacon

        # Pre-set the stop event so run_forever's loop exits immediately.
        svc._stop_event.set()
        svc.run_forever()

        mock_beacon.mark_offline.assert_called_once()
