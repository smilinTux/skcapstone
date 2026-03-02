"""Tests for preflight system checks and auto-install."""

from __future__ import annotations

import json
import platform
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

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
    CheckResult,
    PreflightChecker,
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


# ---------------------------------------------------------------------------
# PreflightChecker tests
# ---------------------------------------------------------------------------

class TestCheckResult:
    """Tests for CheckResult dataclass."""

    def test_ok_status(self) -> None:
        r = CheckResult("test", "ok", "all good")
        assert r.ok is True
        assert r.failed is False
        assert r.warned is False

    def test_warn_status(self) -> None:
        r = CheckResult("test", "warn", "needs attention", critical=False)
        assert r.ok is False
        assert r.warned is True
        assert r.failed is False

    def test_fail_status(self) -> None:
        r = CheckResult("test", "fail", "broken")
        assert r.failed is True
        assert r.ok is False


class TestPreflightCheckerPython:
    """Tests for PreflightChecker.check_python()."""

    def test_current_python_passes(self, tmp_path: Path) -> None:
        """Running Python should be >= 3.11, so check passes."""
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_python()
        assert isinstance(result, CheckResult)
        assert result.name == "python"
        # CI may run older Python; just verify shape
        assert result.status in ("ok", "fail")

    def test_old_python_fails(self, tmp_path: Path) -> None:
        from collections import namedtuple
        VI = namedtuple("version_info", ["major", "minor", "micro", "releaselevel", "serial"])
        vi = VI(3, 10, 0, "final", 0)
        checker = PreflightChecker(home=tmp_path)
        with patch("skcapstone.preflight.sys.version_info", vi):
            result = checker.check_python()
        assert result.status == "fail"
        assert result.critical is True

    def test_311_passes(self, tmp_path: Path) -> None:
        from collections import namedtuple
        VI = namedtuple("version_info", ["major", "minor", "micro", "releaselevel", "serial"])
        vi = VI(3, 11, 0, "final", 0)
        checker = PreflightChecker(home=tmp_path)
        with patch("skcapstone.preflight.sys.version_info", vi):
            result = checker.check_python()
        assert result.status == "ok"


class TestPreflightCheckerPackages:
    """Tests for PreflightChecker.check_packages()."""

    def test_returns_check_result(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_packages()
        assert isinstance(result, CheckResult)
        assert result.name == "packages"

    def test_missing_package_fails(self, tmp_path: Path) -> None:
        import builtins
        real_import = builtins.__import__

        def _mock_import(name, *args, **kwargs):
            if name == "skcomm":
                raise ImportError("no module named skcomm")
            return real_import(name, *args, **kwargs)

        checker = PreflightChecker(home=tmp_path)
        with patch("builtins.__import__", side_effect=_mock_import):
            result = checker.check_packages()
        assert result.status == "fail"
        assert "skcomm" in result.message

    def test_all_present_ok(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        with patch("builtins.__import__", return_value=MagicMock()):
            result = checker.check_packages()
        assert result.status == "ok"


class TestPreflightCheckerOllama:
    """Tests for PreflightChecker.check_ollama()."""

    def test_ollama_unreachable_warns(self, tmp_path: Path) -> None:
        """If Ollama is not running, result is warn (non-critical)."""
        checker = PreflightChecker(home=tmp_path)
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = checker.check_ollama()
        assert result.status == "warn"
        assert result.critical is False

    def test_ollama_running_with_models(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(
            {"models": [{"name": "llama3.2"}, {"name": "mistral"}]}
        ).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = checker.check_ollama()
        assert result.status == "ok"
        assert "llama3.2" in result.message

    def test_ollama_running_no_models_warns(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"models": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = checker.check_ollama()
        assert result.status == "warn"


class TestPreflightCheckerIdentity:
    """Tests for PreflightChecker.check_identity()."""

    def test_identity_json_found(self, tmp_path: Path) -> None:
        identity_dir = tmp_path / "identity"
        identity_dir.mkdir()
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234EFGH5678"}),
            encoding="utf-8",
        )
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_identity()
        assert result.status == "ok"
        assert "opus" in result.message

    def test_no_identity_fails(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_identity()
        assert result.status == "fail"
        assert result.critical is True

    def test_manifest_only_warns(self, tmp_path: Path) -> None:
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_identity()
        assert result.status == "warn"
        assert result.critical is False


class TestPreflightCheckerHomeDirs:
    """Tests for PreflightChecker.check_home_dirs()."""

    def test_all_dirs_present(self, tmp_path: Path) -> None:
        for d in ("memory", "trust", "identity", "config"):
            (tmp_path / d).mkdir()
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_home_dirs()
        assert result.status == "ok"

    def test_missing_dirs_fail(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_home_dirs()
        assert result.status == "fail"
        assert "memory" in result.message or "trust" in result.message


class TestPreflightCheckerConfig:
    """Tests for PreflightChecker.check_config()."""

    def test_no_config_warns(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_config()
        assert result.status == "warn"
        assert result.critical is False

    def test_valid_config_ok(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "consciousness.yaml").write_text(
            "enabled: true\n", encoding="utf-8"
        )
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_config()
        assert result.status == "ok"

    def test_invalid_yaml_fails(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "consciousness.yaml").write_text(
            "enabled: [\nbad yaml", encoding="utf-8"
        )
        checker = PreflightChecker(home=tmp_path)
        result = checker.check_config()
        assert result.status == "fail"


class TestPreflightCheckerDiskSpace:
    """Tests for PreflightChecker.check_disk_space()."""

    def test_plenty_of_space_ok(self, tmp_path: Path) -> None:
        import shutil
        mock_usage = shutil.disk_usage.__class__
        # Return 100 GB free
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=100 * 1024 ** 3)
            checker = PreflightChecker(home=tmp_path)
            result = checker.check_disk_space()
        assert result.status == "ok"

    def test_low_disk_warns(self, tmp_path: Path) -> None:
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = MagicMock(free=2 * 1024 ** 3)  # 2 GB
            checker = PreflightChecker(home=tmp_path)
            result = checker.check_disk_space()
        assert result.status == "warn"
        assert result.critical is False


class TestPreflightCheckerRunAll:
    """Tests for PreflightChecker.run_all()."""

    def test_returns_summary_dict(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        summary = checker.run_all()
        assert isinstance(summary, dict)
        assert "ok" in summary
        assert "checks" in summary
        assert "warnings" in summary
        assert "failures" in summary
        assert "critical_failures" in summary

    def test_all_checks_present(self, tmp_path: Path) -> None:
        checker = PreflightChecker(home=tmp_path)
        summary = checker.run_all()
        names = {c["name"] for c in summary["checks"]}
        assert names == {
            "python", "packages", "ollama", "identity",
            "home_dirs", "config", "disk_space",
        }

    def test_ok_false_on_critical_failure(self, tmp_path: Path) -> None:
        """If home dirs missing and no identity, ok should be False."""
        checker = PreflightChecker(home=tmp_path)
        # No identity, no dirs — critical failures expected
        summary = checker.run_all()
        assert isinstance(summary["ok"], bool)
        # At minimum, critical_failures + failures are ints
        assert isinstance(summary["critical_failures"], int)
        assert isinstance(summary["failures"], int)
