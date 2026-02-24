"""Tests for shell tab completion module.

Covers:
- Script generation for bash, zsh, fish
- Shell detection from environment
- Install to correct paths
- Uninstall removes files
- Invalid shell raises ValueError
- CLI commands (install, show, uninstall)
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.completions import (
    SUPPORTED_SHELLS,
    detect_shell,
    generate_script,
    install_completions,
    uninstall_completions,
)


class TestGenerateScript:
    """Test completion script generation."""

    def test_bash_script(self):
        """Bash script contains the correct env var."""
        script = generate_script("bash")
        assert "_SKCAPSTONE_COMPLETE=bash_source" in script
        assert "skcapstone" in script

    def test_zsh_script(self):
        """Zsh script contains the correct env var."""
        script = generate_script("zsh")
        assert "_SKCAPSTONE_COMPLETE=zsh_source" in script

    def test_fish_script(self):
        """Fish script contains the correct env var."""
        script = generate_script("fish")
        assert "_SKCAPSTONE_COMPLETE=fish_source" in script

    def test_unsupported_shell(self):
        """Unsupported shell raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported"):
            generate_script("powershell")

    def test_all_shells_generate(self):
        """Every supported shell produces a non-empty script."""
        for shell in SUPPORTED_SHELLS:
            script = generate_script(shell)
            assert len(script) > 10


class TestDetectShell:
    """Test shell auto-detection."""

    def test_detect_bash(self):
        """Detects bash from SHELL env."""
        with patch.dict(os.environ, {"SHELL": "/bin/bash"}):
            assert detect_shell() == "bash"

    def test_detect_zsh(self):
        """Detects zsh from SHELL env."""
        with patch.dict(os.environ, {"SHELL": "/usr/bin/zsh"}):
            assert detect_shell() == "zsh"

    def test_detect_fish(self):
        """Detects fish from SHELL env."""
        with patch.dict(os.environ, {"SHELL": "/usr/bin/fish"}):
            assert detect_shell() == "fish"

    def test_unknown_shell(self):
        """Returns None for unknown shells."""
        with patch.dict(os.environ, {"SHELL": "/bin/csh"}):
            assert detect_shell() is None


class TestInstall:
    """Test completion installation."""

    def test_install_bash(self, tmp_path):
        """Install creates bash completion file."""
        with patch("skcapstone.completions.INSTALL_PATHS",
                    {"bash": tmp_path / "skcapstone.bash-completion"}), \
             patch("skcapstone.completions.RC_MARKERS", {}):
            result = install_completions(shell="bash")

        assert result["success"]
        assert result["shell"] == "bash"
        assert (tmp_path / "skcapstone.bash-completion").exists()

    def test_install_auto_detect_fails(self):
        """Install fails gracefully when shell can't be detected."""
        with patch.dict(os.environ, {"SHELL": "/bin/unknown"}):
            result = install_completions(shell=None)
            assert not result["success"]
            assert "error" in result


class TestUninstall:
    """Test completion removal."""

    def test_uninstall_removes_file(self, tmp_path):
        """Uninstall removes the completion script."""
        script = tmp_path / "skcapstone.bash-completion"
        script.write_text("# completion")

        with patch("skcapstone.completions.INSTALL_PATHS",
                    {"bash": script}):
            result = uninstall_completions(shell="bash")

        assert not script.exists()
        assert len(result["removed"]) == 1

    def test_uninstall_no_files(self, tmp_path):
        """Uninstall on empty is a no-op."""
        with patch("skcapstone.completions.INSTALL_PATHS",
                    {"bash": tmp_path / "nonexistent"}):
            result = uninstall_completions(shell="bash")

        assert result["removed"] == []


class TestCLI:
    """Test CLI commands."""

    def test_completions_help(self):
        """completions --help works."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["completions", "--help"])
        assert result.exit_code == 0
        assert "install" in result.output
        assert "show" in result.output
        assert "uninstall" in result.output

    def test_show_bash(self):
        """completions show --shell bash prints script."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["completions", "show", "--shell", "bash"])
        assert result.exit_code == 0
        assert "_SKCAPSTONE_COMPLETE" in result.output

    def test_show_zsh(self):
        """completions show --shell zsh prints script."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["completions", "show", "--shell", "zsh"])
        assert result.exit_code == 0
        assert "zsh_source" in result.output
