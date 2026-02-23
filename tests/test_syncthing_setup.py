"""Tests for the Syncthing setup skill."""

from unittest.mock import patch
from pathlib import Path

from skcapstone.skills.syncthing_setup import (
    detect_syncthing,
    get_install_instructions,
    ensure_shared_folder,
)


class TestDetectSyncthing:
    """Tests for detect_syncthing."""

    @patch("shutil.which", return_value="/usr/bin/syncthing")
    def test_found(self, mock_which):
        """Returns path when syncthing is installed."""
        assert detect_syncthing() == "/usr/bin/syncthing"

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


class TestEnsureSharedFolder:
    """Tests for ensure_shared_folder."""

    def test_creates_directories(self, tmp_path, monkeypatch):
        """Creates outbox, inbox, archive subdirectories."""
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNC_DIR",
            tmp_path / "sync",
        )
        from skcapstone.skills.syncthing_setup import ensure_shared_folder

        result = ensure_shared_folder()
        assert (result / "outbox").exists()
        assert (result / "inbox").exists()
        assert (result / "archive").exists()

    def test_idempotent(self, tmp_path, monkeypatch):
        """Calling twice doesn't fail."""
        monkeypatch.setattr(
            "skcapstone.skills.syncthing_setup.SYNC_DIR",
            tmp_path / "sync",
        )
        from skcapstone.skills.syncthing_setup import ensure_shared_folder

        ensure_shared_folder()
        ensure_shared_folder()
        assert (tmp_path / "sync" / "outbox").exists()
