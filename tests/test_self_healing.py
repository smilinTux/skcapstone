"""Tests for the self-healing doctor."""

from __future__ import annotations

import json
from pathlib import Path

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
