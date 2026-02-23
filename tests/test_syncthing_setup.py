"""Tests for the Syncthing setup skill — Sovereign Singularity."""

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.skills.syncthing_setup import (
    SHARED_FOLDER_ID,
    STIGNORE_CONTENTS,
    configure_syncthing_folder,
    detect_syncthing,
    ensure_shared_folder,
    get_install_instructions,
)


class TestDetectSyncthing:
    """Tests for detect_syncthing."""

    @patch("shutil.which", return_value="/usr/bin/syncthing")
    def test_found(self, mock_which):
        """Returns path when syncthing is installed."""
        assert detect_syncthing() is not None

    @patch("shutil.which", return_value=None)
    def test_not_found(self, mock_which):
        """Returns None when syncthing is not installed."""
        assert detect_syncthing() is None


class TestGetInstallInstructions:
    """Tests for get_install_instructions."""

    @patch("platform.system", return_value="Linux")
    def test_returns_string(self, mock_sys):
        """Always returns a non-empty string."""
        instructions = get_install_instructions()
        assert isinstance(instructions, str)
        assert len(instructions) > 0

    @patch("platform.system", return_value="Darwin")
    def test_macos_mentions_brew(self, mock_sys):
        """macOS instructions mention brew."""
        instructions = get_install_instructions()
        assert "brew" in instructions

    @patch("platform.system", return_value="Windows")
    def test_windows_mentions_winget(self, mock_sys):
        """Windows instructions mention winget."""
        instructions = get_install_instructions()
        assert "winget" in instructions


def _patch_homes(monkeypatch, tmp_path):
    """Set AGENT_HOME and SYNC_DIR to a temp directory for testing."""
    agent_home = tmp_path / ".skcapstone"
    sync_dir = agent_home / "sync"
    monkeypatch.setattr("skcapstone.skills.syncthing_setup.AGENT_HOME", agent_home)
    monkeypatch.setattr("skcapstone.skills.syncthing_setup.SYNC_DIR", sync_dir)
    return agent_home, sync_dir


class TestEnsureSharedFolder:
    """Tests for ensure_shared_folder — creates the full agent home."""

    def test_creates_all_pillar_directories(self, tmp_path, monkeypatch):
        """Creates every pillar data directory under agent home."""
        agent_home, _ = _patch_homes(monkeypatch, tmp_path)

        result = ensure_shared_folder()

        assert result == agent_home
        assert (agent_home / "identity").is_dir()
        assert (agent_home / "memory" / "short-term").is_dir()
        assert (agent_home / "memory" / "mid-term").is_dir()
        assert (agent_home / "memory" / "long-term").is_dir()
        assert (agent_home / "trust" / "febs").is_dir()
        assert (agent_home / "security").is_dir()
        assert (agent_home / "coordination" / "tasks").is_dir()
        assert (agent_home / "coordination" / "agents").is_dir()
        assert (agent_home / "config").is_dir()
        assert (agent_home / "skills").is_dir()

    def test_creates_sync_seed_directories(self, tmp_path, monkeypatch):
        """Creates the sync seed outbox/inbox/archive."""
        agent_home, sync_dir = _patch_homes(monkeypatch, tmp_path)

        ensure_shared_folder()

        assert (sync_dir / "outbox").is_dir()
        assert (sync_dir / "inbox").is_dir()
        assert (sync_dir / "archive").is_dir()

    def test_creates_stignore(self, tmp_path, monkeypatch):
        """Creates .stignore to protect private keys."""
        agent_home, _ = _patch_homes(monkeypatch, tmp_path)

        ensure_shared_folder()

        stignore = agent_home / ".stignore"
        assert stignore.exists()
        contents = stignore.read_text()
        assert "*.key" in contents
        assert "*.pem" in contents
        assert "__pycache__" in contents

    def test_idempotent(self, tmp_path, monkeypatch):
        """Calling twice doesn't fail or corrupt anything."""
        agent_home, _ = _patch_homes(monkeypatch, tmp_path)

        ensure_shared_folder()
        ensure_shared_folder()

        assert (agent_home / "identity").is_dir()
        assert (agent_home / ".stignore").exists()

    def test_returns_agent_home_not_sync_dir(self, tmp_path, monkeypatch):
        """ensure_shared_folder returns agent home (the Syncthing share root)."""
        agent_home, sync_dir = _patch_homes(monkeypatch, tmp_path)

        result = ensure_shared_folder()

        assert result == agent_home
        assert result != sync_dir


class TestConfigureSyncthingFolder:
    """Tests for configure_syncthing_folder — Syncthing XML config."""

    def _make_config(self, tmp_path, existing_folder=None):
        """Create a minimal Syncthing config.xml for testing."""
        config_path = tmp_path / "config.xml"
        root = ET.Element("configuration")

        if existing_folder:
            folder = ET.SubElement(root, "folder")
            for k, v in existing_folder.items():
                folder.set(k, v)

        tree = ET.ElementTree(root)
        tree.write(str(config_path), xml_declaration=True)
        return config_path

    def test_adds_folder_pointing_at_agent_home(self, tmp_path, monkeypatch):
        """New folder in config points at ~/.skcapstone, not sync/."""
        agent_home, _ = _patch_homes(monkeypatch, tmp_path)
        config_path = self._make_config(tmp_path)
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNCTHING_CONFIG_FILE",
            config_path,
        )

        assert configure_syncthing_folder() is True

        tree = ET.parse(config_path)
        folders = list(tree.getroot().iter("folder"))
        assert len(folders) == 1
        assert folders[0].get("id") == SHARED_FOLDER_ID
        assert folders[0].get("path") == str(agent_home)
        assert folders[0].get("label") == "SKCapstone Sovereign"

    def test_upgrades_old_sync_dir_path(self, tmp_path, monkeypatch):
        """Existing folder pointing at sync/ gets upgraded to agent home."""
        agent_home, sync_dir = _patch_homes(monkeypatch, tmp_path)
        config_path = self._make_config(
            tmp_path,
            existing_folder={
                "id": SHARED_FOLDER_ID,
                "label": "SKCapstone Sync",
                "path": str(sync_dir),
            },
        )
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNCTHING_CONFIG_FILE",
            config_path,
        )

        assert configure_syncthing_folder() is True

        tree = ET.parse(config_path)
        folder = list(tree.getroot().iter("folder"))[0]
        assert folder.get("path") == str(agent_home)
        assert folder.get("label") == "SKCapstone Sovereign"

    def test_already_correct_path_is_noop(self, tmp_path, monkeypatch):
        """Folder already pointing at agent home returns True without writing."""
        agent_home, _ = _patch_homes(monkeypatch, tmp_path)
        config_path = self._make_config(
            tmp_path,
            existing_folder={
                "id": SHARED_FOLDER_ID,
                "path": str(agent_home),
            },
        )
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNCTHING_CONFIG_FILE",
            config_path,
        )

        assert configure_syncthing_folder() is True

    def test_no_config_file_returns_false(self, tmp_path, monkeypatch):
        """Returns False when Syncthing config doesn't exist."""
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNCTHING_CONFIG_FILE",
            tmp_path / "nonexistent.xml",
        )

        assert configure_syncthing_folder() is False

    def test_corrupt_config_returns_false(self, tmp_path, monkeypatch):
        """Returns False for unparseable XML."""
        bad_config = tmp_path / "config.xml"
        bad_config.write_text("not xml at all")
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNCTHING_CONFIG_FILE",
            bad_config,
        )

        assert configure_syncthing_folder() is False
