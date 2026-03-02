"""Tests for skcapstone version command and doctor --verbose.

Covers:
- gather_version_info() returns expected keys
- _check_optional_dep() returns version or None
- _probe_ollama() running / not-running paths
- _get_daemon_pid() running / not-running paths
- version CLI: normal output and --json-out
- doctor CLI: --verbose mode shows all checks
- doctor CLI: --verbose with --json-out includes all checks
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Fully initialised agent home with minimal required files."""
    home = tmp_path / ".skcapstone"
    for d in [
        "identity", "memory", "trust", "security", "sync", "config",
        "memory/short-term", "memory/mid-term", "memory/long-term",
    ]:
        (home / d).mkdir(parents=True, exist_ok=True)

    (home / "manifest.json").write_text(json.dumps({
        "name": "TestAgent", "version": "0.1.0",
    }))
    (home / "identity" / "identity.json").write_text(json.dumps({
        "name": "TestAgent",
        "fingerprint": "DEADBEEF12345678",
        "capauth_managed": True,
    }))
    return home


# ---------------------------------------------------------------------------
# Unit tests for version_cmd helpers
# ---------------------------------------------------------------------------


class TestCheckOptionalDep:
    """Tests for _check_optional_dep()."""

    def test_installed_package_returns_version(self):
        """Returns version string when package is importable."""
        from skcapstone.cli.version_cmd import _check_optional_dep

        # 'sys' is always importable; give it a fake __version__ to confirm
        with patch("importlib.import_module") as mock_import:
            mock_mod = MagicMock()
            mock_mod.__version__ = "9.9.9"
            mock_import.return_value = mock_mod
            result = _check_optional_dep("fakepkg")

        assert result == "9.9.9"

    def test_missing_package_returns_none(self):
        """Returns None when package raises ImportError."""
        from skcapstone.cli.version_cmd import _check_optional_dep

        with patch("importlib.import_module", side_effect=ImportError("no module")):
            result = _check_optional_dep("nonexistent_pkg_xyz")

        assert result is None

    def test_package_without_version_attr_returns_installed(self):
        """Returns 'installed' fallback when __version__ is absent."""
        from skcapstone.cli.version_cmd import _check_optional_dep

        with patch("importlib.import_module") as mock_import:
            mock_mod = MagicMock(spec=[])  # no attributes
            mock_import.return_value = mock_mod
            result = _check_optional_dep("nover_pkg")

        assert result == "installed"


class TestProbeOllama:
    """Tests for _probe_ollama()."""

    def test_running_returns_models(self):
        """Running Ollama: running=True, models list populated."""
        from skcapstone.cli.version_cmd import _probe_ollama

        payload = json.dumps({
            "models": [{"name": "llama3:latest"}, {"name": "phi3:mini"}]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _probe_ollama()

        assert result["running"] is True
        assert "llama3:latest" in result["models"]
        assert "phi3:mini" in result["models"]

    def test_not_running_on_connection_error(self):
        """Connection refused: running=False, models=[]."""
        from skcapstone.cli.version_cmd import _probe_ollama

        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = _probe_ollama()

        assert result["running"] is False
        assert result["models"] == []

    def test_host_included_in_result(self, monkeypatch):
        """Custom OLLAMA_HOST appears in the returned dict."""
        from skcapstone.cli.version_cmd import _probe_ollama

        monkeypatch.setenv("OLLAMA_HOST", "http://my-server:11434")
        with patch("urllib.request.urlopen", side_effect=OSError):
            result = _probe_ollama()

        assert result["host"] == "http://my-server:11434"


class TestGetDaemonPid:
    """Tests for _get_daemon_pid()."""

    def test_returns_pid_when_running(self, agent_home: Path):
        """Returns integer PID when daemon is alive."""
        from skcapstone.cli.version_cmd import _get_daemon_pid

        with patch("skcapstone.daemon.read_pid", return_value=99999):
            result = _get_daemon_pid(agent_home)

        assert result == 99999

    def test_returns_none_when_stopped(self, agent_home: Path):
        """Returns None when no PID file exists."""
        from skcapstone.cli.version_cmd import _get_daemon_pid

        with patch("skcapstone.daemon.read_pid", return_value=None):
            result = _get_daemon_pid(agent_home)

        assert result is None

    def test_returns_none_on_exception(self, agent_home: Path):
        """Swallows import or runtime errors, returns None."""
        from skcapstone.cli.version_cmd import _get_daemon_pid

        with patch("skcapstone.daemon.read_pid", side_effect=RuntimeError("oops")):
            result = _get_daemon_pid(agent_home)

        assert result is None


class TestGatherVersionInfo:
    """Tests for gather_version_info()."""

    def test_contains_all_expected_keys(self, agent_home: Path):
        """Dict has all required top-level keys."""
        from skcapstone.cli.version_cmd import gather_version_info

        with patch("urllib.request.urlopen", side_effect=OSError), \
             patch("skcapstone.daemon.read_pid", return_value=None):
            info = gather_version_info(agent_home)

        assert "package_version" in info
        assert "python_version" in info
        assert "platform" in info
        assert "optional_deps" in info
        assert "ollama" in info
        assert "daemon_pid" in info

    def test_optional_deps_has_four_packages(self, agent_home: Path):
        """optional_deps covers watchdog, skcomm, skchat, skseed."""
        from skcapstone.cli.version_cmd import gather_version_info

        with patch("urllib.request.urlopen", side_effect=OSError), \
             patch("skcapstone.daemon.read_pid", return_value=None):
            info = gather_version_info(agent_home)

        deps = info["optional_deps"]
        assert set(deps.keys()) == {"watchdog", "skcomm", "skchat", "skseed"}

    def test_package_version_matches_module(self, agent_home: Path):
        """package_version matches skcapstone.__version__."""
        from skcapstone import __version__
        from skcapstone.cli.version_cmd import gather_version_info

        with patch("urllib.request.urlopen", side_effect=OSError), \
             patch("skcapstone.daemon.read_pid", return_value=None):
            info = gather_version_info(agent_home)

        assert info["package_version"] == __version__


# ---------------------------------------------------------------------------
# CLI integration tests — version command
# ---------------------------------------------------------------------------


class TestVersionCommand:
    """Integration tests for `skcapstone version`."""

    def _run(self, args: list[str], agent_home: Path):
        from skcapstone.cli import main

        runner = CliRunner(mix_stderr=False)
        with patch("urllib.request.urlopen", side_effect=OSError), \
             patch("skcapstone.daemon.read_pid", return_value=None):
            return runner.invoke(
                main,
                ["version", "--home", str(agent_home)] + args,
                catch_exceptions=False,
            )

    def test_default_output_contains_version(self, agent_home: Path):
        """Normal output includes the skcapstone package version."""
        from skcapstone import __version__

        result = self._run([], agent_home)
        assert result.exit_code == 0, result.output
        assert __version__ in result.output

    def test_default_output_lists_optional_deps(self, agent_home: Path):
        """Normal output lists all four optional dep names."""
        result = self._run([], agent_home)
        assert result.exit_code == 0
        for pkg in ("watchdog", "skcomm", "skchat", "skseed"):
            assert pkg in result.output

    def test_json_output_is_valid_and_complete(self, agent_home: Path):
        """--json-out emits valid JSON with all required keys."""
        result = self._run(["--json-out"], agent_home)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "package_version" in data
        assert "python_version" in data
        assert "optional_deps" in data
        assert "ollama" in data
        assert "daemon_pid" in data

    def test_daemon_running_shown_in_output(self, agent_home: Path):
        """Shows running + PID when daemon is alive."""
        from skcapstone.cli import main

        runner = CliRunner(mix_stderr=False)
        with patch("urllib.request.urlopen", side_effect=OSError), \
             patch("skcapstone.daemon.read_pid", return_value=42001):
            result = runner.invoke(
                main,
                ["version", "--home", str(agent_home)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "42001" in result.output

    def test_ollama_running_shows_model_count(self, agent_home: Path):
        """When Ollama is up, output includes model count."""
        from skcapstone.cli import main

        payload = json.dumps({
            "models": [{"name": "llama3:latest"}, {"name": "mistral:7b"}]
        }).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = payload

        runner = CliRunner(mix_stderr=False)
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("skcapstone.daemon.read_pid", return_value=None):
            result = runner.invoke(
                main,
                ["version", "--home", str(agent_home)],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert "2 model" in result.output


# ---------------------------------------------------------------------------
# CLI integration tests — doctor --verbose
# ---------------------------------------------------------------------------


class TestDoctorVerbose:
    """Integration tests for `skcapstone doctor --verbose`."""

    def _run_doctor(self, args: list[str], agent_home: Path):
        from skcapstone.cli import main

        runner = CliRunner(mix_stderr=False)
        return runner.invoke(
            main,
            ["doctor", "--home", str(agent_home)] + args,
            catch_exceptions=False,
        )

    def test_verbose_shows_passing_checks(self, agent_home: Path):
        """--verbose prints checks that passed, not just failures."""
        result = self._run_doctor(["--verbose"], agent_home)
        assert result.exit_code == 0
        # At minimum the home:exists check should be present in output
        assert "Agent home directory" in result.output

    def test_verbose_output_includes_check_names(self, agent_home: Path):
        """--verbose output contains internal check names in parentheses."""
        result = self._run_doctor(["--verbose"], agent_home)
        assert result.exit_code == 0
        # Internal names like (home:exists) appear in verbose mode
        assert "home:exists" in result.output

    def test_verbose_shows_summary_line(self, agent_home: Path):
        """--verbose ends with a 'Summary:' line containing pass/fail counts."""
        result = self._run_doctor(["--verbose"], agent_home)
        assert result.exit_code == 0
        assert "Summary:" in result.output
        assert "passed" in result.output

    def test_non_verbose_collapses_all_pass_categories(self, agent_home: Path):
        """Without --verbose, fully-passing categories are on one line."""
        result = self._run_doctor([], agent_home)
        assert result.exit_code == 0
        # Agent Home directory exists, so it should be collapsed
        assert "passed" in result.output

    def test_verbose_json_includes_all_checks(self, agent_home: Path):
        """--verbose --json-out still emits the full checks list."""
        result = self._run_doctor(["--verbose", "--json-out"], agent_home)
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "checks" in data
        assert len(data["checks"]) > 0
        # Every check has a name
        for c in data["checks"]:
            assert "name" in c

    def test_verbose_help_text_mentions_verbose(self, agent_home: Path):
        """--help output documents the --verbose flag."""
        from skcapstone.cli import main

        runner = CliRunner(mix_stderr=False)
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "--verbose" in result.output
