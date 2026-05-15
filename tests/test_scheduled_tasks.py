"""Tests for skcapstone.scheduled_tasks — cron-like scheduler."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from skcapstone.scheduled_tasks import (
    ScheduledTask,
    TaskScheduler,
    build_scheduler,
    make_backend_reprobe_task,
    make_heartbeat_task,
    make_memory_promotion_task,
    make_profile_freshness_task,
)


# ---------------------------------------------------------------------------
# ScheduledTask unit tests
# ---------------------------------------------------------------------------


class TestScheduledTaskIsDue:
    def test_never_run_is_always_due(self):
        task = ScheduledTask(name="x", interval_seconds=60, callback=lambda: None)
        assert task.is_due() is True

    def test_recently_run_is_not_due(self):
        task = ScheduledTask(name="x", interval_seconds=60, callback=lambda: None)
        task.last_run = datetime.now(timezone.utc)
        assert task.is_due() is False

    def test_overdue_is_due(self):
        task = ScheduledTask(name="x", interval_seconds=60, callback=lambda: None)
        task.last_run = datetime.now(timezone.utc) - timedelta(seconds=61)
        assert task.is_due() is True

    def test_exactly_at_interval_is_due(self):
        task = ScheduledTask(name="x", interval_seconds=60, callback=lambda: None)
        task.last_run = datetime.now(timezone.utc) - timedelta(seconds=60)
        assert task.is_due() is True

    def test_custom_now_reference(self):
        task = ScheduledTask(name="x", interval_seconds=60, callback=lambda: None)
        task.last_run = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        future = datetime(2026, 1, 1, 12, 2, 0, tzinfo=timezone.utc)  # 120s later
        assert task.is_due(now=future) is True


class TestScheduledTaskRun:
    def test_successful_run_increments_count(self):
        calls = []
        task = ScheduledTask(name="t", interval_seconds=1, callback=lambda: calls.append(1))
        task.run()
        assert task.run_count == 1
        assert task.error_count == 0
        assert task.last_error is None
        assert len(calls) == 1

    def test_successful_run_sets_last_run(self):
        before = datetime.now(timezone.utc)
        task = ScheduledTask(name="t", interval_seconds=1, callback=lambda: None)
        task.run()
        after = datetime.now(timezone.utc)
        assert task.last_run is not None
        assert before <= task.last_run <= after

    def test_failed_run_records_error(self):
        def _boom():
            raise ValueError("something broke")

        task = ScheduledTask(name="t", interval_seconds=1, callback=_boom)
        task.run()
        assert task.error_count == 1
        assert task.run_count == 0
        assert "something broke" in task.last_error

    def test_failed_run_still_updates_last_run(self):
        """last_run must be set even when the callback raises, so the interval resets."""
        task = ScheduledTask(name="t", interval_seconds=1, callback=lambda: 1 / 0)
        task.run()
        assert task.last_run is not None

    def test_successive_runs_accumulate_count(self):
        counter = {"n": 0}

        def _inc():
            counter["n"] += 1

        task = ScheduledTask(name="t", interval_seconds=0, callback=_inc)
        for _ in range(5):
            task.run()
        assert task.run_count == 5
        assert counter["n"] == 5

    def test_cleared_error_after_recovery(self):
        state = {"fail": True}

        def _flaky():
            if state["fail"]:
                raise RuntimeError("transient")

        task = ScheduledTask(name="t", interval_seconds=0, callback=_flaky)
        task.run()
        assert task.last_error is not None

        state["fail"] = False
        task.run()
        assert task.last_error is None
        assert task.run_count == 1
        assert task.error_count == 1


# ---------------------------------------------------------------------------
# TaskScheduler tests
# ---------------------------------------------------------------------------


class TestTaskSchedulerRegister:
    def test_register_returns_task(self, tmp_path):
        stop = threading.Event()
        scheduler = TaskScheduler(tmp_path, stop)
        task = scheduler.register("ping", 10, lambda: None)
        assert isinstance(task, ScheduledTask)
        assert task.name == "ping"
        assert task.interval_seconds == 10

    def test_register_multiple_tasks(self, tmp_path):
        stop = threading.Event()
        scheduler = TaskScheduler(tmp_path, stop)
        scheduler.register("a", 5, lambda: None)
        scheduler.register("b", 10, lambda: None)
        scheduler.register("c", 15, lambda: None)
        assert len(scheduler.status()) == 3

    def test_status_reflects_unrun_tasks(self, tmp_path):
        stop = threading.Event()
        scheduler = TaskScheduler(tmp_path, stop)
        scheduler.register("mine", 30, lambda: None)
        status = scheduler.status()
        assert status[0]["name"] == "mine"
        assert status[0]["last_run"] is None
        assert status[0]["run_count"] == 0
        assert status[0]["error_count"] == 0


class TestTaskSchedulerExecution:
    def test_scheduler_runs_due_task(self, tmp_path):
        """Scheduler fires a task with 0-second interval within a short window."""
        stop = threading.Event()
        fired = threading.Event()

        scheduler = TaskScheduler(tmp_path, stop, tick_interval=0.05)
        scheduler.register("instant", 0, fired.set)
        scheduler.start()

        assert fired.wait(timeout=2.0), "Task should have fired within 2 seconds"
        stop.set()

    def test_scheduler_stops_cleanly(self, tmp_path):
        stop = threading.Event()
        scheduler = TaskScheduler(tmp_path, stop, tick_interval=0.05)
        scheduler.register("noop", 999, lambda: None)
        t = scheduler.start()
        stop.set()
        t.join(timeout=2.0)
        assert not t.is_alive()

    def test_scheduler_thread_is_daemon(self, tmp_path):
        stop = threading.Event()
        scheduler = TaskScheduler(tmp_path, stop, tick_interval=1)
        t = scheduler.start()
        assert t.daemon is True
        stop.set()

    def test_scheduler_skips_not_yet_due_task(self, tmp_path):
        """A task last run just now should NOT fire again within the tick window."""
        stop = threading.Event()
        counter = {"n": 0}

        def _count():
            counter["n"] += 1

        scheduler = TaskScheduler(tmp_path, stop, tick_interval=0.05)
        task = scheduler.register("slow", 3600, _count)
        # Pre-mark as just run so it is NOT due
        task.last_run = datetime.now(timezone.utc)

        scheduler.start()
        time.sleep(0.2)
        stop.set()

        assert counter["n"] == 0, "Task should not have fired — it was just run"

    def test_error_in_task_does_not_crash_scheduler(self, tmp_path):
        """A raising callback must not kill the scheduler thread."""
        stop = threading.Event()
        second_fired = threading.Event()

        def _bad():
            raise RuntimeError("intentional")

        scheduler = TaskScheduler(tmp_path, stop, tick_interval=0.05)
        scheduler.register("bad", 0, _bad)
        scheduler.register("good", 0, second_fired.set)
        scheduler.start()

        assert second_fired.wait(timeout=2.0), "Scheduler should survive a bad task"
        stop.set()


# ---------------------------------------------------------------------------
# build_scheduler — registration completeness and intervals
# ---------------------------------------------------------------------------


class TestBuildScheduler:
    def test_registers_four_standard_tasks(self, tmp_path):
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        names = {s["name"] for s in scheduler.status()}
        assert names == {
            "heartbeat_pulse",
            "backend_reprobe",
            "memory_promotion_sweep",
            "profile_freshness_check",
        }

    def test_heartbeat_interval_is_30s(self, tmp_path):
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        task = next(s for s in scheduler.status() if s["name"] == "heartbeat_pulse")
        assert task["interval_seconds"] == 30

    def test_backend_reprobe_interval_is_5min(self, tmp_path):
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        task = next(s for s in scheduler.status() if s["name"] == "backend_reprobe")
        assert task["interval_seconds"] == 300

    def test_memory_promotion_interval_is_hourly(self, tmp_path):
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        task = next(s for s in scheduler.status() if s["name"] == "memory_promotion_sweep")
        assert task["interval_seconds"] == 3600

    def test_profile_freshness_interval_is_daily(self, tmp_path):
        stop = threading.Event()
        scheduler = build_scheduler(tmp_path, stop)
        task = next(s for s in scheduler.status() if s["name"] == "profile_freshness_check")
        assert task["interval_seconds"] == 86400


# ---------------------------------------------------------------------------
# Individual task callback tests
# ---------------------------------------------------------------------------


class TestMemoryPromotionTask:
    def test_calls_sweep_and_logs_promotions(self, tmp_path):
        mock_result = MagicMock()
        mock_result.scanned = 10
        mock_result.promoted = [MagicMock(), MagicMock()]  # 2 promoted

        mock_engine = MagicMock()
        mock_engine.sweep.return_value = mock_result

        callback = make_memory_promotion_task(tmp_path)

        # PromotionEngine is imported lazily inside the closure via
        # `from .memory_promoter import PromotionEngine` — patch the source.
        with patch("skcapstone.memory_promoter.PromotionEngine", return_value=mock_engine) as MockEngine:
            callback()
            MockEngine.assert_called_once_with(tmp_path)
            mock_engine.sweep.assert_called_once()

    def test_no_promotions_does_not_raise(self, tmp_path):
        mock_result = MagicMock()
        mock_result.scanned = 5
        mock_result.promoted = []

        mock_engine = MagicMock()
        mock_engine.sweep.return_value = mock_result

        callback = make_memory_promotion_task(tmp_path)
        with patch("skcapstone.memory_promoter.PromotionEngine", return_value=mock_engine):
            callback()  # should not raise

    def test_import_error_propagates_as_exception(self, tmp_path):
        """If PromotionEngine raises on import the task should propagate (caught by runner)."""
        callback = make_memory_promotion_task(tmp_path)
        with patch("skcapstone.memory_promoter.PromotionEngine", side_effect=RuntimeError("unavailable")):
            with pytest.raises(RuntimeError, match="unavailable"):
                callback()


class TestBackendReprobeTask:
    def test_calls_probe_on_bridge(self):
        mock_bridge = MagicMock()
        mock_bridge._available = {"ollama": True, "passthrough": True}

        mock_loop = MagicMock()
        mock_loop._bridge = mock_bridge

        callback = make_backend_reprobe_task(mock_loop)
        callback()
        mock_bridge._probe_available_backends.assert_called_once()

    def test_noop_when_loop_is_none(self):
        callback = make_backend_reprobe_task(None)
        callback()  # should not raise

    def test_noop_when_bridge_missing(self):
        mock_loop = MagicMock(spec=[])  # no _bridge attribute
        callback = make_backend_reprobe_task(mock_loop)
        callback()  # should not raise

    def test_noop_when_probe_fn_missing(self):
        mock_bridge = MagicMock(spec=[])  # no _probe_available_backends
        mock_loop = MagicMock()
        mock_loop._bridge = mock_bridge
        callback = make_backend_reprobe_task(mock_loop)
        callback()  # should not raise


class TestHeartbeatTask:
    def test_calls_pulse_with_active_state(self):
        mock_beacon = MagicMock()
        callback = make_heartbeat_task(mock_beacon, lambda: True)
        callback()
        mock_beacon.pulse.assert_called_once_with(consciousness_active=True)

    def test_calls_pulse_with_inactive_state(self):
        mock_beacon = MagicMock()
        callback = make_heartbeat_task(mock_beacon, lambda: False)
        callback()
        mock_beacon.pulse.assert_called_once_with(consciousness_active=False)

    def test_noop_when_beacon_is_none(self):
        callback = make_heartbeat_task(None, lambda: True)
        callback()  # should not raise

    def test_uses_fn_result_dynamically(self):
        """consciousness_active_fn is called each time, not captured at build time."""
        mock_beacon = MagicMock()
        state = {"active": False}
        callback = make_heartbeat_task(mock_beacon, lambda: state["active"])

        callback()
        mock_beacon.pulse.assert_called_with(consciousness_active=False)

        state["active"] = True
        callback()
        mock_beacon.pulse.assert_called_with(consciousness_active=True)


class TestProfileFreshnessTask:
    def test_fresh_files_produce_no_warning(self, tmp_path, caplog):
        import logging

        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text("{}")

        callback = make_profile_freshness_task(tmp_path, max_age_days=7)
        with caplog.at_level(logging.WARNING, logger="skcapstone.scheduled_tasks"):
            callback()
        assert not any("Profile freshness" in r.message for r in caplog.records)

    def test_stale_identity_triggers_warning(self, tmp_path, caplog):
        import logging
        import os

        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        identity_file = identity_dir / "identity.json"
        identity_file.write_text("{}")

        # Set mtime to 10 days ago
        old_mtime = time.time() - (10 * 86400)
        os.utime(identity_file, (old_mtime, old_mtime))

        callback = make_profile_freshness_task(tmp_path, max_age_days=7)
        with caplog.at_level(logging.WARNING, logger="skcapstone.scheduled_tasks"):
            callback()

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("identity.json" in w for w in warnings)

    def test_stale_model_profile_triggers_warning(self, tmp_path, caplog):
        import logging
        import os

        profiles_dir = tmp_path / "data" / "model_profiles"
        profiles_dir.mkdir(parents=True)
        profile_file = profiles_dir / "llama3.json"
        profile_file.write_text("{}")

        old_mtime = time.time() - (15 * 86400)
        os.utime(profile_file, (old_mtime, old_mtime))

        callback = make_profile_freshness_task(tmp_path, max_age_days=7)
        with caplog.at_level(logging.WARNING, logger="skcapstone.scheduled_tasks"):
            callback()

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("llama3" in w for w in warnings)

    def test_missing_identity_dir_does_not_raise(self, tmp_path):
        callback = make_profile_freshness_task(tmp_path)
        callback()  # identity dir absent — should not raise

    def test_missing_profiles_dir_does_not_raise(self, tmp_path):
        callback = make_profile_freshness_task(tmp_path)
        callback()  # data/model_profiles absent — should not raise

    def test_custom_max_age_respected(self, tmp_path, caplog):
        import logging
        import os

        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        identity_file = identity_dir / "identity.json"
        identity_file.write_text("{}")

        # 3 days old
        old_mtime = time.time() - (3 * 86400)
        os.utime(identity_file, (old_mtime, old_mtime))

        # max_age_days=2 → should warn
        callback = make_profile_freshness_task(tmp_path, max_age_days=2)
        with caplog.at_level(logging.WARNING, logger="skcapstone.scheduled_tasks"):
            callback()

        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("identity.json" in w for w in warnings)

        # max_age_days=5 → should NOT warn
        caplog.clear()
        callback2 = make_profile_freshness_task(tmp_path, max_age_days=5)
        with caplog.at_level(logging.WARNING, logger="skcapstone.scheduled_tasks"):
            callback2()

        warnings2 = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("identity.json" in w for w in warnings2)
