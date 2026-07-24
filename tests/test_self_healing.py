"""Tests for the self-healing doctor."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skcapstone.self_healing import SelfHealingDoctor


class TestHomeDirs:
    """Home directory auto-fix tests."""

    def test_creates_missing_dirs(self, tmp_path):
        """Missing home subdirs are auto-created."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        # Only create 'identity' — rest should be auto-fixed
        (home / "identity").mkdir()

        doctor = SelfHealingDoctor(home)
        result = doctor._check_home_dirs()

        assert result["status"] == "fixed"
        assert (home / "memory").exists()
        assert (home / "trust").exists()
        assert (home / "config").exists()

    def test_ok_when_all_present(self, tmp_path):
        """Returns ok when all dirs exist."""
        home = tmp_path / ".skcapstone"
        for subdir in ("identity", "memory", "trust", "security", "sync", "config", "soul", "logs"):
            (home / subdir).mkdir(parents=True)

        doctor = SelfHealingDoctor(home)
        result = doctor._check_home_dirs()
        assert result["status"] == "ok"


class TestMemoryIndex:
    """Memory index rebuild tests."""

    def test_rebuilds_missing_index(self, tmp_path):
        """Missing index.json is rebuilt from memory files."""
        home = tmp_path / ".skcapstone"
        memory_dir = home / "memory" / "short-term"
        memory_dir.mkdir(parents=True)

        # Write a test memory file
        memory = {
            "memory_id": "test-123",
            "content": "Test memory",
            "tags": ["test"],
        }
        (memory_dir / "test-123.json").write_text(json.dumps(memory))

        doctor = SelfHealingDoctor(home)
        result = doctor._check_memory_index()

        assert result["status"] == "fixed"
        index = json.loads((home / "memory" / "index.json").read_text())
        assert len(index) == 1
        assert index[0]["memory_id"] == "test-123"

    def test_ok_when_valid(self, tmp_path):
        """Returns ok when index exists and is valid."""
        home = tmp_path / ".skcapstone"
        memory_dir = home / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "index.json").write_text("[]")

        doctor = SelfHealingDoctor(home)
        result = doctor._check_memory_index()
        assert result["status"] == "ok"


class TestSyncManifest:
    """Sync manifest auto-fix tests."""

    def test_creates_default_manifest(self, tmp_path):
        """Missing sync-manifest.json is auto-created."""
        home = tmp_path / ".skcapstone"
        sync_dir = home / "sync"
        sync_dir.mkdir(parents=True)

        doctor = SelfHealingDoctor(home)
        result = doctor._check_sync_manifest()

        assert result["status"] == "fixed"
        manifest = json.loads((sync_dir / "sync-manifest.json").read_text())
        assert manifest["version"] == 1
        assert "syncthing" in manifest["backends"]

    def test_ok_when_present(self, tmp_path):
        """Returns ok when manifest exists."""
        home = tmp_path / ".skcapstone"
        sync_dir = home / "sync"
        sync_dir.mkdir(parents=True)
        (sync_dir / "sync-manifest.json").write_text("{}")

        doctor = SelfHealingDoctor(home)
        result = doctor._check_sync_manifest()
        assert result["status"] == "ok"


class TestConsciousnessHealth:
    """Consciousness health check tests."""

    def test_ok_when_not_loaded(self, tmp_path):
        """Returns ok when consciousness is not loaded (disabled)."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        doctor = SelfHealingDoctor(home, consciousness_loop=None)
        result = doctor._check_consciousness_health()
        assert result["status"] == "ok"
        assert "not loaded" in result["message"]


class TestProfileFreshness:
    """Profile freshness check tests."""

    def test_checks_profile_dates(self, tmp_path):
        """Profile freshness check doesn't crash."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        doctor = SelfHealingDoctor(home)
        result = doctor._check_profile_freshness()
        assert result["status"] == "ok"


class TestDiagnoseAndHeal:
    """Full diagnose-and-heal pipeline tests."""

    def test_full_pipeline(self, tmp_path):
        """Full pipeline runs all checks and returns report."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        doctor = SelfHealingDoctor(home)
        report = doctor.diagnose_and_heal()

        assert "checks_run" in report
        assert "checks_passed" in report
        assert "auto_fixed" in report
        assert "still_broken" in report
        assert report["checks_run"] > 0
        assert isinstance(report["details"], list)

    def test_all_ok_when_healthy(self, tmp_path):
        """Healthy home returns all checks passed."""
        home = tmp_path / ".skcapstone"
        for subdir in ("identity", "memory", "trust", "security", "sync", "config", "soul", "logs"):
            (home / subdir).mkdir(parents=True)
        (home / "memory" / "index.json").write_text("[]")
        (home / "sync" / "sync-manifest.json").write_text("{}")

        doctor = SelfHealingDoctor(home)
        report = doctor.diagnose_and_heal()

        assert report["still_broken"] == 0
        assert report["checks_passed"] == report["checks_run"]

    def test_last_report_saved(self, tmp_path):
        """Last report is accessible after running."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        doctor = SelfHealingDoctor(home)
        doctor.diagnose_and_heal()
        assert doctor.last_report["checks_run"] > 0


class TestInotifyRestart:
    """Regression tests for card 934eae16.

    self_healing._check_consciousness_health() calls
    consciousness._run_inotify_restart() when it detects a dead inotify
    observer. Before the fix that method did not exist and the call raised
    AttributeError, so healing crashed instead of restarting the watcher.
    """

    def _make_loop(self):
        """Build a bare ConsciousnessLoop without running heavy __init__."""
        from skcapstone.consciousness_loop import ConsciousnessLoop

        return object.__new__(ConsciousnessLoop)

    def test_restart_stops_old_observer_and_starts_new_watcher(self, monkeypatch):
        """_run_inotify_restart stops the dead observer and relaunches _run_inotify."""
        loop = self._make_loop()
        old_obs = MagicMock()
        loop._observer = old_obs

        relaunched = threading.Event()
        monkeypatch.setattr(loop, "_run_inotify", relaunched.set)

        loop._run_inotify_restart()

        # Old observer torn down.
        old_obs.stop.assert_called_once()
        old_obs.join.assert_called_once()
        assert loop._observer is None

        # New watcher thread was launched and ran _run_inotify.
        assert relaunched.wait(timeout=2.0), "_run_inotify was not relaunched"

    def test_restart_with_no_observer_still_relaunches(self, monkeypatch):
        """_run_inotify_restart is safe when there is no prior observer."""
        loop = self._make_loop()
        loop._observer = None

        relaunched = threading.Event()
        monkeypatch.setattr(loop, "_run_inotify", relaunched.set)

        # Must not raise even with nothing to stop.
        loop._run_inotify_restart()

        assert relaunched.wait(timeout=2.0), "_run_inotify was not relaunched"

    def _fake_consciousness(self, observer):
        """Consciousness stub with a reachable backend and given observer."""
        bridge = SimpleNamespace(available_backends={"local": True})
        return SimpleNamespace(
            _bridge=bridge,
            _observer=observer,
            _run_inotify_restart=MagicMock(),
        )

    def test_dead_observer_triggers_restart(self, tmp_path):
        """A dead inotify observer is the triggering event -> restart is called."""
        dead_observer = MagicMock()
        dead_observer.is_alive.return_value = False
        consciousness = self._fake_consciousness(dead_observer)

        doctor = SelfHealingDoctor(tmp_path / ".skcapstone", consciousness_loop=consciousness)
        result = doctor._check_consciousness_health()

        consciousness._run_inotify_restart.assert_called_once()
        assert result["status"] == "ok"

    def test_live_observer_is_noop(self, tmp_path):
        """A healthy observer must NOT trigger a restart (no-op path)."""
        live_observer = MagicMock()
        live_observer.is_alive.return_value = True
        consciousness = self._fake_consciousness(live_observer)

        doctor = SelfHealingDoctor(tmp_path / ".skcapstone", consciousness_loop=consciousness)
        result = doctor._check_consciousness_health()

        consciousness._run_inotify_restart.assert_not_called()
        assert result["status"] == "ok"
