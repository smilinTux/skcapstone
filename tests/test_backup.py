"""Tests for sovereign agent backup and restore."""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from skcapstone.backup import (
    BackupManifest,
    create_backup,
    list_backups,
    restore_backup,
)


def _setup_agent_home(tmp_path: Path) -> Path:
    """Create a fake agent home directory with test data.

    Args:
        tmp_path: Temporary directory from pytest.

    Returns:
        Path: The fake agent home.
    """
    home = tmp_path / ".skcapstone"

    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text('agent_name: "TestAgent"\n')

    (home / "identity").mkdir()
    (home / "identity" / "profile.json").write_text('{"name": "TestAgent"}')
    (home / "identity" / "public.asc").write_text("-----BEGIN PGP PUBLIC KEY-----\ntest\n-----END PGP PUBLIC KEY-----\n")

    (home / "memory").mkdir()
    (home / "memory" / "mem1.json").write_text('{"id": "mem1", "title": "test memory"}')
    (home / "memory" / "mem2.json").write_text('{"id": "mem2", "title": "another memory"}')

    (home / "trust").mkdir()
    (home / "trust" / "FEB_test.feb").write_text('{"emotional_payload": {}}')

    (home / "coordination" / "tasks").mkdir(parents=True)
    (home / "coordination" / "tasks" / "task1.json").write_text('{"status": "done"}')

    (home / "manifest.json").write_text('{"name": "TestAgent", "version": "0.1.0"}')
    (home / "agent-card.json").write_text('{"name": "TestAgent", "fingerprint": "abc123"}')

    return home


class TestCreateBackup:
    """Tests for backup creation."""

    def test_create_basic_backup(self, tmp_path: Path) -> None:
        """Happy path: backup creates a valid tar.gz archive."""
        home = _setup_agent_home(tmp_path)
        out_dir = tmp_path / "backups"

        result = create_backup(home=home, output_dir=out_dir, agent_name="TestAgent")

        assert result["file_count"] > 0
        assert result["archive_size"] > 0
        assert Path(result["filepath"]).exists()
        assert result["filepath"].endswith(".tar.gz")

    def test_backup_contains_expected_files(self, tmp_path: Path) -> None:
        """Archive contains identity, memory, config, and manifest."""
        home = _setup_agent_home(tmp_path)
        result = create_backup(home=home, output_dir=tmp_path / "out")

        with tarfile.open(result["filepath"], "r:gz") as tar:
            names = tar.getnames()

        has_config = any("config/config.yaml" in n for n in names)
        has_identity = any("identity/profile.json" in n for n in names)
        has_memory = any("memory/mem1.json" in n for n in names)
        has_manifest = any("manifest.json" in n for n in names)

        assert has_config
        assert has_identity
        assert has_memory
        assert has_manifest

    def test_backup_manifest_has_checksums(self, tmp_path: Path) -> None:
        """Manifest contains SHA-256 checksums for all files."""
        home = _setup_agent_home(tmp_path)
        result = create_backup(home=home, output_dir=tmp_path / "out")

        manifest = result["manifest"]
        assert len(manifest["files"]) > 0
        for filepath, checksum in manifest["files"].items():
            assert len(checksum) == 64

    def test_backup_excludes_pycache(self, tmp_path: Path) -> None:
        """Backup skips __pycache__ directories."""
        home = _setup_agent_home(tmp_path)
        cache_dir = home / "memory" / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "module.cpython-312.pyc").write_text("bytecode")

        result = create_backup(home=home, output_dir=tmp_path / "out")

        with tarfile.open(result["filepath"], "r:gz") as tar:
            names = tar.getnames()

        assert not any("__pycache__" in n for n in names)
        assert not any(".pyc" in n for n in names)

    def test_backup_missing_home_raises(self, tmp_path: Path) -> None:
        """Backup raises FileNotFoundError for missing home."""
        with pytest.raises(FileNotFoundError):
            create_backup(home=tmp_path / "nonexistent")


class TestRestoreBackup:
    """Tests for backup restoration."""

    def test_restore_roundtrip(self, tmp_path: Path) -> None:
        """Backup then restore produces identical files."""
        home = _setup_agent_home(tmp_path)
        result = create_backup(home=home, output_dir=tmp_path / "out")

        restore_target = tmp_path / "restored"
        restore_result = restore_backup(
            archive_path=result["filepath"],
            target_home=restore_target,
        )

        assert restore_result["file_count"] > 0
        assert restore_result["verified"] is True
        assert len(restore_result["errors"]) == 0

        assert (restore_target / "config" / "config.yaml").exists()
        assert (restore_target / "identity" / "profile.json").exists()
        assert (restore_target / "memory" / "mem1.json").exists()

    def test_restore_detects_tampered_file(self, tmp_path: Path) -> None:
        """Verification catches files that don't match manifest checksums."""
        home = _setup_agent_home(tmp_path)
        result = create_backup(home=home, output_dir=tmp_path / "out")

        restore_target = tmp_path / "tampered"
        restore_backup(
            archive_path=result["filepath"],
            target_home=restore_target,
            verify=False,
        )

        # Tamper after extraction, then verify by comparing to manifest
        (restore_target / "memory" / "mem1.json").write_text("TAMPERED!")

        from skcapstone.backup import BackupManifest, _sha256_file

        manifest = BackupManifest(**result["manifest"])
        errors = []
        for rel_path, expected in manifest.files.items():
            f = restore_target / rel_path
            if f.exists() and _sha256_file(f) != expected:
                errors.append(rel_path)

        assert len(errors) > 0
        assert any("mem1" in e for e in errors)

    def test_restore_missing_archive_raises(self, tmp_path: Path) -> None:
        """Restore raises FileNotFoundError for missing archive."""
        with pytest.raises(FileNotFoundError):
            restore_backup(archive_path="/nonexistent.tar.gz")

    def test_restore_no_verify(self, tmp_path: Path) -> None:
        """Restore with verify=False skips checksum checks."""
        home = _setup_agent_home(tmp_path)
        result = create_backup(home=home, output_dir=tmp_path / "out")

        restore_target = tmp_path / "noverify"
        restore_result = restore_backup(
            archive_path=result["filepath"],
            target_home=restore_target,
            verify=False,
        )
        assert restore_result["file_count"] > 0


class TestListBackups:
    """Tests for backup listing."""

    def test_list_empty_dir(self, tmp_path: Path) -> None:
        """Empty directory returns empty list."""
        assert list_backups(tmp_path) == []

    def test_list_nonexistent_dir(self, tmp_path: Path) -> None:
        """Missing directory returns empty list."""
        assert list_backups(tmp_path / "nope") == []

    def test_list_with_backups(self, tmp_path: Path) -> None:
        """Lists backup archives sorted newest first."""
        home = _setup_agent_home(tmp_path)
        out = tmp_path / "backups"

        create_backup(home=home, output_dir=out)
        create_backup(home=home, output_dir=out)

        backups = list_backups(out)
        assert len(backups) == 2
        assert all(b["filename"].endswith(".tar.gz") for b in backups)
        assert all(b["size"] > 0 for b in backups)


class TestBackupManifest:
    """Tests for the manifest model."""

    def test_manifest_defaults(self) -> None:
        """Manifest has sensible defaults."""
        m = BackupManifest()
        assert m.version == "0.1.0"
        assert m.files == {}
        assert m.total_size == 0

    def test_manifest_serialization(self) -> None:
        """Manifest roundtrips through JSON."""
        m = BackupManifest(
            backup_id="test-123",
            agent_name="Jarvis",
            files={"config.yaml": "abc123"},
        )
        data = json.loads(m.model_dump_json())
        loaded = BackupManifest(**data)
        assert loaded.backup_id == "test-123"
        assert loaded.files["config.yaml"] == "abc123"
