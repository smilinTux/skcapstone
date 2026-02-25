"""Tests for preflight system checks and auto-install."""

from __future__ import annotations

import platform
from unittest.mock import patch, MagicMock

import pytest

from skcapstone.preflight import (
    GIT_DOWNLOAD_DEFAULT,
    GIT_DOWNLOAD_URLS,
    GitPreflightResult,
    PreflightResult,
    ToolCheck,
    ToolStatus,
    auto_install_tool,
    check_git,
    check_gpg,
    check_python,
    check_syncthing,
    git_install_hint_for_doctor,
    run_preflight,
)


class TestCheckPython:
    """Tests for check_python()."""

    def test_returns_toolcheck(self) -> None:
        """Returns a ToolCheck with installed status."""
        result = check_python()
        assert isinstance(result, ToolCheck)
        assert result.name == "Python"
        assert result.installed is True
        assert result.required is True

    def test_has_version(self) -> None:
        """Version string contains major.minor."""
        result = check_python()
        assert "3." in result.version


class TestCheckGpg:
    """Tests for check_gpg()."""

    def test_returns_toolcheck(self) -> None:
        """Returns a ToolCheck."""
        result = check_gpg()
        assert isinstance(result, ToolCheck)
        assert result.name == "GnuPG"
        assert result.required is True

    @patch("skcapstone.preflight.shutil.which", return_value=None)
    def test_missing_has_install_info(self, mock_which: MagicMock) -> None:
        """When missing, provides install command and download URL."""
        result = check_gpg()
        assert result.status == ToolStatus.MISSING
        assert result.download_url != ""


class TestCheckGit:
    """Tests for check_git()."""

    def test_returns_toolcheck(self) -> None:
        """Returns a ToolCheck."""
        result = check_git()
        assert isinstance(result, ToolCheck)
        assert result.name == "Git"

    def test_not_required_by_default(self) -> None:
        """Git is not required by default."""
        result = check_git(required=False)
        assert result.required is False

    def test_required_when_specified(self) -> None:
        """Git is required when flag is True."""
        result = check_git(required=True)
        assert result.required is True

    @patch("skcapstone.preflight.shutil.which", return_value=None)
    def test_missing_has_install_cmd(self, mock_which: MagicMock) -> None:
        """When missing, provides platform-specific install command."""
        result = check_git(required=True)
        assert result.status == ToolStatus.MISSING
        assert result.install_cmd != "" or result.download_url != ""


class TestCheckSyncthing:
    """Tests for check_syncthing()."""

    def test_returns_toolcheck(self) -> None:
        """Returns a ToolCheck."""
        result = check_syncthing()
        assert isinstance(result, ToolCheck)
        assert result.name == "Syncthing"

    @patch("skcapstone.preflight.shutil.which", return_value=None)
    def test_missing_has_download_url(self, mock_which: MagicMock) -> None:
        """When missing, provides download URL."""
        result = check_syncthing()
        assert result.status == ToolStatus.MISSING
        assert "syncthing.net" in result.download_url


class TestRunPreflight:
    """Tests for run_preflight()."""

    def test_returns_preflight_result(self) -> None:
        """Returns a PreflightResult with all checks."""
        result = run_preflight()
        assert isinstance(result, PreflightResult)
        assert isinstance(result.python, ToolCheck)
        assert isinstance(result.gpg, ToolCheck)
        assert isinstance(result.git, ToolCheck)
        assert isinstance(result.syncthing, ToolCheck)

    def test_all_ok_when_optional_missing(self) -> None:
        """all_ok is True when only optional tools are missing."""
        result = run_preflight(require_git=False, require_syncthing=False)
        if result.python.installed and result.gpg.installed:
            assert result.all_ok is True

    def test_required_missing_list(self) -> None:
        """required_missing lists only required missing tools."""
        result = run_preflight()
        for check in result.required_missing:
            assert check.required is True
            assert check.installed is False


class TestToolCheck:
    """Tests for ToolCheck properties."""

    def test_installed_property(self) -> None:
        """installed is True when status is INSTALLED."""
        check = ToolCheck(name="Test", status=ToolStatus.INSTALLED, required=True)
        assert check.installed is True
        assert check.ok is True

    def test_missing_required(self) -> None:
        """ok is False when required and missing."""
        check = ToolCheck(name="Test", status=ToolStatus.MISSING, required=True)
        assert check.installed is False
        assert check.ok is False

    def test_missing_optional(self) -> None:
        """ok is True when optional and missing."""
        check = ToolCheck(name="Test", status=ToolStatus.MISSING, required=False)
        assert check.installed is False
        assert check.ok is True


class TestAutoInstallTool:
    """Tests for auto_install_tool()."""

    def test_already_installed(self) -> None:
        """Returns True if tool is already installed."""
        check = ToolCheck(name="Test", status=ToolStatus.INSTALLED, required=True)
        assert auto_install_tool(check) is True

    def test_no_install_cmd(self) -> None:
        """Returns False if no install command available."""
        check = ToolCheck(name="Test", status=ToolStatus.MISSING, required=True, install_cmd="")
        assert auto_install_tool(check) is False

    @patch("skcapstone.preflight.subprocess.run")
    def test_runs_install_cmd(self, mock_run: MagicMock) -> None:
        """Runs the install command when provided."""
        mock_run.return_value = MagicMock(returncode=0)
        check = ToolCheck(
            name="Test", status=ToolStatus.MISSING, required=True,
            install_cmd="sudo apt install -y test-tool",
        )
        result = auto_install_tool(check)
        assert result is True
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------

class TestGitDownloadUrls:
    """Tests for legacy platform download URL mapping."""

    def test_windows_url(self) -> None:
        assert "Windows" in GIT_DOWNLOAD_URLS
        assert "git-scm.com" in GIT_DOWNLOAD_URLS["Windows"]

    def test_linux_url(self) -> None:
        assert "Linux" in GIT_DOWNLOAD_URLS
        assert "git-scm.com" in GIT_DOWNLOAD_URLS["Linux"]

    def test_darwin_url(self) -> None:
        assert "Darwin" in GIT_DOWNLOAD_URLS
        assert "git-scm.com" in GIT_DOWNLOAD_URLS["Darwin"]

    def test_default_url_exists(self) -> None:
        assert "git-scm.com" in GIT_DOWNLOAD_DEFAULT


class TestGitPreflightResult:
    """Tests for legacy GitPreflightResult."""

    def test_run_returns_result(self) -> None:
        r = GitPreflightResult.run()
        assert isinstance(r.installed, bool)
        assert isinstance(r.platform_label, str)
        assert isinstance(r.message, str)
        assert isinstance(r.download_url, str)

    @patch("skcapstone.preflight.shutil.which", return_value=None)
    def test_not_installed_has_url(self, mock_which: MagicMock) -> None:
        """When git is missing, download_url is populated."""
        r = GitPreflightResult.run()
        assert r.installed is False
        assert "git-scm.com" in r.download_url


class TestGitInstallHintForDoctor:
    """Tests for legacy git_install_hint_for_doctor()."""

    def test_returns_string(self) -> None:
        hint = git_install_hint_for_doctor()
        assert isinstance(hint, str)
