"""Tests for skcapstone init (onboard alias) and skcapstone reset commands."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main


# ---------------------------------------------------------------------------
# skcapstone init — alias for onboard
# ---------------------------------------------------------------------------


class TestInitAlias:
    """Verify that `skcapstone init` delegates to run_onboard."""

    def test_init_calls_run_onboard(self, tmp_path: Path) -> None:
        """init command invokes run_onboard with the home path."""
        runner = CliRunner()
        with patch("skcapstone.onboard.run_onboard") as mock_onboard:
            result = runner.invoke(main, ["init", "--home", str(tmp_path)])
        mock_onboard.assert_called_once_with(str(tmp_path))
        assert result.exit_code == 0

    def test_init_registered_in_cli(self) -> None:
        """init is a registered top-level command."""
        assert "init" in main.commands

    def test_init_and_onboard_both_registered(self) -> None:
        """Both init and onboard exist as distinct commands."""
        assert "init" in main.commands
        assert "onboard" in main.commands

    def test_init_default_home(self) -> None:
        """init uses AGENT_HOME when --home is not supplied."""
        from skcapstone.cli._common import AGENT_HOME

        runner = CliRunner()
        with patch("skcapstone.onboard.run_onboard") as mock_onboard:
            result = runner.invoke(main, ["init"])
        # run_onboard should have been called with the default home string
        mock_onboard.assert_called_once_with(AGENT_HOME)
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# skcapstone reset
# ---------------------------------------------------------------------------


class TestReset:
    """Tests for `skcapstone reset` factory-reset command."""

    def _make_home(self, tmp_path: Path) -> Path:
        """Build a minimal agent home tree with an identity dir."""
        home = tmp_path / ".skcapstone"
        (home / "identity").mkdir(parents=True)
        (home / "identity" / "key.gpg").write_text("fake-key-data")
        (home / "memory").mkdir()
        (home / "config").mkdir()
        return home

    def test_reset_aborted_on_wrong_confirmation(self, tmp_path: Path) -> None:
        """Reset is aborted when the user types anything other than YES."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home)],
            input="no\n",
        )
        assert result.exit_code == 0
        assert "aborted" in result.output.lower()
        assert home.exists(), "Home should NOT be deleted when aborted"

    def test_reset_aborted_on_empty_confirmation(self, tmp_path: Path) -> None:
        """Reset is aborted when the user presses Enter (empty input)."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home)],
            input="\n",
        )
        assert result.exit_code == 0
        assert "aborted" in result.output.lower()
        assert home.exists()

    def test_reset_wipes_home_on_yes_confirmation(self, tmp_path: Path) -> None:
        """Typing YES wipes the agent home directory."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home)],
            input="YES\n",
        )
        assert result.exit_code == 0
        assert not home.exists(), "Home should be deleted after YES"
        assert "reset complete" in result.output.lower()

    def test_reset_backs_up_identity_before_wipe(self, tmp_path: Path) -> None:
        """identity/ is copied to a timestamped backup before deletion."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home)],
            input="YES\n",
        )
        assert result.exit_code == 0

        # Find backup dir(s) created under tmp_path
        backups = list(tmp_path.glob(".skcapstone-backup-*"))
        assert len(backups) == 1, f"Expected one backup dir, found: {backups}"
        backup = backups[0]
        assert (backup / "identity" / "key.gpg").exists(), (
            "identity/key.gpg must be present in the backup"
        )

    def test_reset_force_skips_prompt(self, tmp_path: Path) -> None:
        """--force bypasses the confirmation prompt."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home), "--force"],
        )
        assert result.exit_code == 0
        assert not home.exists(), "Home should be wiped with --force"

    def test_reset_nonexistent_home_is_graceful(self, tmp_path: Path) -> None:
        """reset on a non-existent home prints a warning and exits cleanly."""
        missing = tmp_path / ".skcapstone-does-not-exist"
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(missing)],
        )
        assert result.exit_code == 0
        assert "nothing to reset" in result.output.lower()

    def test_reset_registered_in_cli(self) -> None:
        """reset is a registered top-level command."""
        assert "reset" in main.commands

    def test_reset_backup_path_in_output(self, tmp_path: Path) -> None:
        """Output mentions the backup path when identity/ exists."""
        home = self._make_home(tmp_path)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reset", "--home", str(home), "--force"],
        )
        assert result.exit_code == 0
        assert "backup" in result.output.lower()
        # Rich may wrap long paths — join lines before checking
        joined = result.output.replace("\n", "")
        assert ".skcapstone-backup-" in joined
