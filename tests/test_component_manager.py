"""Tests for ComponentHealth and ComponentManager in skcapstone.daemon."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from skcapstone.daemon import ComponentHealth, ComponentManager

# ---------------------------------------------------------------------------
# ComponentHealth unit tests
# ---------------------------------------------------------------------------


class TestComponentHealth:
    """Tests for the ComponentHealth state machine."""

    def test_initial_state(self):
        """New component starts in pending state with no timestamps."""
        comp = ComponentHealth("poll")
        assert comp.status == "pending"
        assert comp.started_at is None
        assert comp.last_heartbeat is None
        assert comp.restart_count == 0
        assert comp.last_error is None

    def test_mark_started_sets_alive(self):
        """mark_started() transitions to alive and records timestamps."""
        comp = ComponentHealth("poll")
        comp.mark_started()
        assert comp.status == "alive"
        assert comp.started_at is not None
        assert comp.last_heartbeat is not None

    def test_pulse_updates_heartbeat(self):
        """pulse() updates last_heartbeat without changing started_at."""
        comp = ComponentHealth("poll")
        comp.mark_started()
        before = comp.last_heartbeat
        time.sleep(0.01)
        comp.pulse()
        assert comp.last_heartbeat > before
        assert comp.started_at == comp.started_at  # unchanged

    def test_pulse_from_non_alive_transitions_to_alive(self):
        """pulse() on a dead component recovers it to alive."""
        comp = ComponentHealth("poll")
        comp.mark_dead("oops")
        assert comp.status == "dead"
        comp.pulse()
        assert comp.status == "alive"

    def test_mark_dead_records_error(self):
        """mark_dead() stores error message and sets status to dead."""
        comp = ComponentHealth("poll")
        comp.mark_started()
        comp.mark_dead("connection refused")
        assert comp.status == "dead"
        assert comp.last_error == "connection refused"

    def test_mark_dead_no_error(self):
        """mark_dead() with no error leaves last_error as None."""
        comp = ComponentHealth("poll")
        comp.mark_dead()
        assert comp.status == "dead"
        assert comp.last_error is None

    def test_mark_restarting_increments_counter(self):
        """mark_restarting() increments restart_count each time."""
        comp = ComponentHealth("poll")
        comp.mark_restarting()
        assert comp.status == "restarting"
        assert comp.restart_count == 1
        comp.mark_restarting()
        assert comp.restart_count == 2

    def test_mark_disabled(self):
        """mark_disabled() sets status to disabled."""
        comp = ComponentHealth("poll")
        comp.mark_disabled()
        assert comp.status == "disabled"

    def test_mark_alive_sets_timestamps(self):
        """mark_alive() sets started_at and last_heartbeat."""
        comp = ComponentHealth("poll")
        comp.mark_alive()
        assert comp.status == "alive"
        assert comp.started_at is not None
        assert comp.last_heartbeat is not None

    def test_snapshot_fields(self):
        """snapshot() returns all required fields."""
        comp = ComponentHealth("health", auto_restart=True, heartbeat_timeout=60)
        comp.mark_started()
        snap = comp.snapshot()
        assert snap["name"] == "health"
        assert snap["status"] == "alive"
        assert snap["auto_restart"] is True
        assert snap["restart_count"] == 0
        assert snap["last_error"] is None
        assert snap["started_at"] is not None
        assert snap["last_heartbeat"] is not None
        assert snap["heartbeat_age_seconds"] is not None
        assert snap["heartbeat_age_seconds"] >= 0

    def test_snapshot_heartbeat_age_none_when_never_pulsed(self):
        """snapshot() returns heartbeat_age_seconds=None before any pulse."""
        comp = ComponentHealth("poll")
        snap = comp.snapshot()
        assert snap["heartbeat_age_seconds"] is None

    def test_thread_safe_concurrent_pulses(self):
        """Multiple threads pulsing simultaneously should not corrupt state."""
        comp = ComponentHealth("poll")
        comp.mark_started()
        errors = []

        def do_pulses():
            try:
                for _ in range(100):
                    comp.pulse()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=do_pulses) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert comp.status == "alive"


# ---------------------------------------------------------------------------
# ComponentManager unit tests
# ---------------------------------------------------------------------------


class TestComponentManager:
    """Tests for ComponentManager registration, heartbeats, and snapshots."""

    def _stop(self) -> threading.Event:
        ev = threading.Event()
        ev.set()  # pre-set so loops exit immediately
        return ev

    def test_register_creates_component(self):
        """register() creates a ComponentHealth entry."""
        mgr = ComponentManager(self._stop())
        mgr.register("poll", lambda: None)
        snap = mgr.snapshot()
        assert "poll" in snap

    def test_register_disabled(self):
        """register(..., disabled=True) creates a disabled component."""
        mgr = ComponentManager(self._stop())
        mgr.register("healing", lambda: None, disabled=True)
        snap = mgr.snapshot()
        assert snap["healing"]["status"] == "disabled"

    def test_register_passive_alive(self):
        """register_passive() creates a passive alive component."""
        mgr = ComponentManager(self._stop())
        mgr.register_passive("consciousness", status="alive")
        snap = mgr.snapshot()
        assert snap["consciousness"]["status"] == "alive"
        assert snap["consciousness"]["auto_restart"] is False

    def test_register_passive_disabled(self):
        """register_passive(..., status='disabled') creates a disabled entry."""
        mgr = ComponentManager(self._stop())
        mgr.register_passive("scheduler", status="disabled")
        snap = mgr.snapshot()
        assert snap["scheduler"]["status"] == "disabled"

    def test_heartbeat_updates_timestamp(self):
        """heartbeat() updates last_heartbeat for a registered component."""
        mgr = ComponentManager(self._stop())
        mgr.register("poll", lambda: None)
        # Manually mark started so heartbeat_age is tracked
        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        before = comp.last_heartbeat

        time.sleep(0.02)
        mgr.heartbeat("poll")

        assert comp.last_heartbeat > before

    def test_heartbeat_unknown_name_no_error(self):
        """heartbeat() on an unknown name should not raise."""
        mgr = ComponentManager(self._stop())
        mgr.heartbeat("nonexistent")  # must not raise

    def test_mark_dead_sets_status(self):
        """mark_dead() transitions component to dead status."""
        mgr = ComponentManager(self._stop())
        mgr.register("poll", lambda: None)
        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        mgr.mark_dead("poll", "test error")
        assert comp.status == "dead"
        assert comp.last_error == "test error"

    def test_mark_alive_on_passive(self):
        """mark_alive() on a passive component sets it alive."""
        mgr = ComponentManager(self._stop())
        mgr.register_passive("consciousness", status="disabled")
        mgr.mark_alive("consciousness")
        snap = mgr.snapshot()
        assert snap["consciousness"]["status"] == "alive"

    def test_mark_disabled_on_passive(self):
        """mark_disabled() on a passive component sets it disabled."""
        mgr = ComponentManager(self._stop())
        mgr.register_passive("scheduler", status="alive")
        mgr.mark_disabled("scheduler")
        snap = mgr.snapshot()
        assert snap["scheduler"]["status"] == "disabled"

    def test_snapshot_returns_all_components(self):
        """snapshot() includes every registered component."""
        mgr = ComponentManager(self._stop())
        mgr.register("poll", lambda: None)
        mgr.register("health", lambda: None)
        mgr.register_passive("consciousness", status="alive")
        snap = mgr.snapshot()
        assert set(snap.keys()) == {"poll", "health", "consciousness"}

    def test_snapshot_is_serializable(self):
        """snapshot() values must be JSON-serializable primitives."""
        import json

        mgr = ComponentManager(self._stop())
        mgr.register("poll", lambda: None)
        mgr.register_passive("consciousness", status="alive")
        snap = mgr.snapshot()
        # Should not raise
        encoded = json.dumps(snap)
        decoded = json.loads(encoded)
        assert "poll" in decoded
        assert "consciousness" in decoded


# ---------------------------------------------------------------------------
# Auto-restart tests via _check_components()
# ---------------------------------------------------------------------------


class TestComponentManagerAutoRestart:
    """Tests for watchdog auto-restart logic via _check_components()."""

    def _make_mgr(self) -> ComponentManager:
        stop = threading.Event()
        return ComponentManager(stop)

    def test_dead_component_is_restarted(self):
        """_check_components() restarts a dead auto-restart component."""
        call_log: list[str] = []

        def loop():
            call_log.append("called")
            # Exit immediately (simulates a restarted loop that exits)

        mgr = self._make_mgr()
        mgr.register("poll", loop)

        # Mark it as already-started, then immediately dead
        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        comp.mark_dead("test crash")

        # Run the watchdog check once
        mgr._check_components()

        # A new thread was launched - give it a moment to run
        time.sleep(0.1)
        assert len(call_log) >= 1
        assert comp.restart_count == 1

    def test_max_restarts_not_exceeded(self):
        """Components exceeding MAX_RESTARTS within the window are not restarted."""
        call_log: list[str] = []

        def loop():
            call_log.append("called")

        mgr = self._make_mgr()
        mgr.register("poll", loop)

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()

        # Exhaust restart budget inside the window
        for _ in range(ComponentManager.MAX_RESTARTS):
            comp.mark_restarting()
        comp.mark_dead("too many times")

        before = len(call_log)
        mgr._check_components()
        time.sleep(0.05)

        assert len(call_log) == before  # no new call
        assert comp.gave_up is True
        assert comp.status == "failed"

    def test_old_restarts_outside_window_do_not_exhaust_budget(self):
        """Restarts older than RESTART_WINDOW don't count against the budget.

        This is the long-uptime bug: a lifetime counter meant a handful of
        transient blips spread over weeks permanently abandoned a component.
        """
        from datetime import timedelta

        call_log: list[str] = []

        def loop():
            call_log.append("called")

        mgr = self._make_mgr()
        mgr.register("poll", loop)

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()

        # MAX_RESTARTS blips, but each one is days old
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=ComponentManager.RESTART_WINDOW + 86400
        )
        comp.restart_times = [stale] * ComponentManager.MAX_RESTARTS
        comp.restart_count = ComponentManager.MAX_RESTARTS
        comp.mark_dead("blip")

        mgr._check_components()
        time.sleep(0.1)

        assert len(call_log) >= 1  # restarted anyway - budget aged out
        assert comp.gave_up is False
        assert comp.restart_times == [comp.last_restart_at]  # stale entries pruned

    def test_give_up_logged_once_not_every_tick(self, caplog):
        """The give-up error logs on transition only, not every watchdog tick."""
        mgr = self._make_mgr()
        mgr.register("poll", lambda: None)

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        for _ in range(ComponentManager.MAX_RESTARTS):
            comp.mark_restarting()
        comp.mark_dead("crash loop")

        with caplog.at_level(logging.ERROR, logger="skcapstone.daemon"):
            mgr._check_components()
            mgr._check_components()
            mgr._check_components()

        assert sum("giving up" in r.getMessage() for r in caplog.records) == 1

    def test_rearm_after_budget_recovers(self):
        """A given-up component restarts again once its window clears."""
        from datetime import timedelta

        call_log: list[str] = []

        mgr = self._make_mgr()
        mgr.register("poll", lambda: call_log.append("called"))

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        for _ in range(ComponentManager.MAX_RESTARTS):
            comp.mark_restarting()
        comp.mark_dead("crash loop")

        mgr._check_components()
        assert comp.gave_up is True
        assert call_log == []

        # Age every restart out of the window
        stale = datetime.now(timezone.utc) - timedelta(
            seconds=ComponentManager.RESTART_WINDOW + 60
        )
        comp.restart_times = [stale] * ComponentManager.MAX_RESTARTS
        comp.last_restart_at = stale
        comp.mark_dead("still down")

        mgr._check_components()
        time.sleep(0.1)

        assert comp.gave_up is False
        assert len(call_log) >= 1

    def test_backoff_blocks_immediate_second_restart(self):
        """Two checks back-to-back don't burn two restarts - backoff gates it."""
        call_log: list[str] = []

        mgr = self._make_mgr()
        mgr.register("poll", lambda: call_log.append("called"))

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()
        comp.mark_dead("crash")

        mgr._check_components()
        time.sleep(0.05)
        first = len(call_log)

        comp.mark_dead("crash again")
        mgr._check_components()  # immediately - inside RESTART_BACKOFF
        time.sleep(0.05)

        assert len(call_log) == first
        assert comp.restart_count == 1

    def test_disabled_component_not_restarted(self):
        """Disabled components are never restarted by the watchdog."""
        call_log: list[str] = []

        def loop():
            call_log.append("called")

        mgr = self._make_mgr()
        mgr.register("healing", loop, disabled=True)

        mgr._check_components()
        time.sleep(0.05)
        assert call_log == []

    def test_passive_component_not_restarted(self):
        """Passive (non-auto-restart) components are never restarted."""
        call_log: list[str] = []

        def loop():
            call_log.append("called")

        mgr = self._make_mgr()
        # Register passive, then forcibly mark dead to test watchdog ignores it
        mgr.register_passive("consciousness", status="alive")
        with mgr._lock:
            comp = mgr._health["consciousness"]
        comp.mark_dead("test")

        mgr._check_components()
        time.sleep(0.05)
        assert call_log == []

    def test_heartbeat_timeout_triggers_restart(self):
        """Component with stale heartbeat is detected and restarted."""
        from datetime import timedelta

        call_log: list[str] = []

        def loop():
            call_log.append("called")

        mgr = self._make_mgr()
        mgr.register("poll", loop, heartbeat_timeout=1)

        with mgr._lock:
            comp = mgr._health["poll"]
        comp.mark_started()

        # Backdate last_heartbeat past the timeout
        comp.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=10)
        comp.status = "alive"

        mgr._check_components()
        time.sleep(0.1)

        assert comp.restart_count >= 1

    def test_alive_thread_not_restarted(self):
        """Component whose thread is still alive is not unnecessarily restarted."""
        stop = threading.Event()
        mgr = ComponentManager(stop)

        started = threading.Event()

        def loop():
            started.set()
            stop.wait()  # block until stop

        mgr.register("poll", loop)
        threads = mgr.start_all()
        started.wait(timeout=2)

        # Give heartbeat a moment to fire
        time.sleep(0.05)
        mgr.heartbeat("poll")

        with mgr._lock:
            comp = mgr._health["poll"]
        original_restarts = comp.restart_count

        mgr._check_components()  # should NOT restart
        assert comp.restart_count == original_restarts

        stop.set()
        for t in threads:
            t.join(timeout=1)
