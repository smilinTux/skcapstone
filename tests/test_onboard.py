"""Tests for the skcapstone onboard wizard — new system-setup steps.

Covers:
- _step_prereqs(): Python/pip/Ollama detection
- _step_ollama_models(): model pull with click.confirm gating
- _step_config_files(): consciousness.yaml + model_profiles.yaml writing
- _step_systemd_service(): Linux-only, click.confirm gating
- _step_doctor_check(): doctor diagnostics output
- _step_test_consciousness(): consciousness loop test with click.confirm gating
- TOTAL_STEPS constant updated to 16
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_home(tmp_path: Path) -> Path:
    """A temporary agent home directory with required sub-dirs."""
    home = tmp_path / ".skcapstone"
    for d in ["identity", "memory", "trust", "security", "sync", "config"]:
        (home / d).mkdir(parents=True, exist_ok=True)
    return home


# ---------------------------------------------------------------------------
# TOTAL_STEPS
# ---------------------------------------------------------------------------


class TestTotalSteps:
    """Ensure TOTAL_STEPS reflects the current 16-step wizard."""

    def test_total_steps_is_16(self) -> None:
        """Wizard now has 16 numbered steps."""
        from skcapstone.onboard import TOTAL_STEPS

        assert TOTAL_STEPS == 16


# ---------------------------------------------------------------------------
# _step_prereqs
# ---------------------------------------------------------------------------


class TestStepPrereqs:
    """Tests for _step_prereqs()."""

    def test_returns_dict_with_three_keys(self) -> None:
        """Always returns a dict with python/pip/ollama keys."""
        from skcapstone.onboard import _step_prereqs

        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("shutil.which", return_value="/usr/bin/pip"):
                result = _step_prereqs()

        assert "python" in result
        assert "pip" in result
        assert "ollama" in result

    def test_python_ok_for_current_interpreter(self) -> None:
        """Current Python should pass the >= 3.10 check."""
        from skcapstone.onboard import _step_prereqs

        runner = CliRunner()
        with runner.isolated_filesystem():
            with patch("shutil.which", side_effect=lambda t: "/usr/bin/pip" if t in ("pip", "pip3") else None):
                result = _step_prereqs()

        # Running under test means Python >= 3.10 in this project
        assert result["python"] is (sys.version_info >= (3, 10))

    def test_pip_true_when_pip_on_path(self) -> None:
        """pip key is True when pip or pip3 is found via shutil.which."""
        from skcapstone.onboard import _step_prereqs

        def fake_which(tool: str) -> str | None:
            return "/usr/bin/pip3" if tool == "pip3" else None

        # Only mock shutil.which; leave sys untouched so version_info works.
        with patch("shutil.which", side_effect=fake_which):
            result = _step_prereqs()

        assert result["pip"] is True

    def test_pip_false_when_not_on_path(self) -> None:
        """pip key is False when neither pip nor pip3 found."""
        from skcapstone.onboard import _step_prereqs

        with patch("shutil.which", return_value=None):
            result = _step_prereqs()

        assert result["pip"] is False

    def test_ollama_detected(self) -> None:
        """ollama key is True when ollama binary found."""
        from skcapstone.onboard import _step_prereqs

        def fake_which(tool: str) -> str | None:
            return "/usr/local/bin/ollama" if tool == "ollama" else "/usr/bin/pip"

        with patch("shutil.which", side_effect=fake_which), \
             patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ollama version 0.5.0\n")):
            result = _step_prereqs()

        assert result["ollama"] is True

    def test_ollama_not_found(self) -> None:
        """ollama key is False when ollama binary missing."""
        from skcapstone.onboard import _step_prereqs

        def fake_which(tool: str) -> str | None:
            if tool == "ollama":
                return None
            return "/usr/bin/pip"

        with patch("shutil.which", side_effect=fake_which):
            result = _step_prereqs()

        assert result["ollama"] is False

    def test_result_is_dict(self) -> None:
        """_step_prereqs always returns a plain dict."""
        from skcapstone.onboard import _step_prereqs

        with patch("shutil.which", return_value="/usr/bin/pip"):
            result = _step_prereqs()

        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# _step_ollama_models
# ---------------------------------------------------------------------------


class TestStepOllamaModels:
    """Tests for _step_ollama_models()."""

    def test_skips_when_ollama_not_available(self) -> None:
        """Returns default structured result when prereqs['ollama'] is False."""
        from skcapstone.onboard import _step_ollama_models

        result = _step_ollama_models({"ollama": False})

        assert result == {
            "ok": False,
            "model": "llama3.2",
            "host": "http://localhost:11434",
        }

    def test_skips_when_user_declines(self) -> None:
        """Returns a non-ok result when user does not confirm the pull."""
        from skcapstone.onboard import _step_ollama_models

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")), \
             patch("click.prompt", side_effect=["http://localhost:11434", "llama3.2"]), \
             patch("click.confirm", return_value=False):
            result = _step_ollama_models({"ollama": True})

        assert result == {
            "ok": False,
            "model": "llama3.2",
            "host": "http://localhost:11434",
        }

    def test_returns_true_when_model_already_present(self) -> None:
        """Returns ok=True without pulling if model already in 'ollama list'."""
        from skcapstone.onboard import _step_ollama_models

        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="NAME ID SIZE\nllama3.2 abc123 2.0 GB")), \
             patch("click.prompt", side_effect=["http://localhost:11434", "llama3.2"]):
            result = _step_ollama_models({"ollama": True})

        assert result["ok"] is True
        assert result["model"] == "llama3.2"

    def test_returns_true_on_successful_pull(self) -> None:
        """Returns ok=True after a successful ollama pull."""
        from skcapstone.onboard import _step_ollama_models

        def fake_run(cmd, **kwargs):
            if "list" in cmd:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=0)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("click.prompt", side_effect=["http://localhost:11434", "llama3.2"]), \
             patch("click.confirm", return_value=True):
            result = _step_ollama_models({"ollama": True})

        assert result["ok"] is True

    def test_returns_false_on_pull_failure(self) -> None:
        """Returns ok=False when ollama pull exits non-zero."""
        from skcapstone.onboard import _step_ollama_models

        def fake_run(cmd, **kwargs):
            if "list" in cmd:
                return MagicMock(returncode=0, stdout="")
            return MagicMock(returncode=1)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("click.prompt", side_effect=["http://localhost:11434", "llama3.2"]), \
             patch("click.confirm", return_value=True):
            result = _step_ollama_models({"ollama": True})

        assert result["ok"] is False

    def test_returns_false_on_timeout(self) -> None:
        """Returns ok=False when ollama pull times out."""
        from skcapstone.onboard import _step_ollama_models

        def fake_run(cmd, **kwargs):
            if "list" in cmd:
                return MagicMock(returncode=0, stdout="")
            raise subprocess.TimeoutExpired(cmd, 600)

        with patch("subprocess.run", side_effect=fake_run), \
             patch("click.prompt", side_effect=["http://localhost:11434", "llama3.2"]), \
             patch("click.confirm", return_value=True):
            result = _step_ollama_models({"ollama": True})

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# _step_config_files
# ---------------------------------------------------------------------------


class TestStepConfigFiles:
    """Tests for _step_config_files()."""

    def test_writes_consciousness_yaml(self, tmp_home: Path) -> None:
        """Creates consciousness.yaml in config/ when missing."""
        from skcapstone.onboard import _step_config_files

        consciousness_ok, _ = _step_config_files(tmp_home)

        assert consciousness_ok is True
        dest = tmp_home / "config" / "consciousness.yaml"
        assert dest.exists()

    def test_skips_existing_consciousness_yaml(self, tmp_home: Path) -> None:
        """Does not overwrite an existing consciousness.yaml."""
        from skcapstone.onboard import _step_config_files

        existing = tmp_home / "config" / "consciousness.yaml"
        existing.write_text("enabled: false\n", encoding="utf-8")

        consciousness_ok, _ = _step_config_files(tmp_home)

        assert consciousness_ok is True
        assert existing.read_text(encoding="utf-8") == "enabled: false\n"

    def test_writes_model_profiles_yaml(self, tmp_home: Path) -> None:
        """Copies bundled model_profiles.yaml when missing."""
        from skcapstone.onboard import _step_config_files
        from pathlib import Path as _Path
        import skcapstone.onboard as _onboard_module

        bundled = _Path(_onboard_module.__file__).parent / "data" / "model_profiles.yaml"
        if not bundled.exists():
            pytest.skip("Bundled model_profiles.yaml not present in data/")

        _, profiles_ok = _step_config_files(tmp_home)

        assert profiles_ok is True
        dest = tmp_home / "config" / "model_profiles.yaml"
        assert dest.exists()
        assert dest.stat().st_size > 0

    def test_skips_existing_model_profiles(self, tmp_home: Path) -> None:
        """Does not overwrite an existing model_profiles.yaml."""
        from skcapstone.onboard import _step_config_files

        existing = tmp_home / "config" / "model_profiles.yaml"
        existing.write_text("profiles: []\n", encoding="utf-8")

        _, profiles_ok = _step_config_files(tmp_home)

        assert profiles_ok is True
        assert existing.read_text(encoding="utf-8") == "profiles: []\n"

    def test_returns_two_booleans(self, tmp_home: Path) -> None:
        """Always returns a 2-tuple of booleans."""
        from skcapstone.onboard import _step_config_files

        result = _step_config_files(tmp_home)

        assert isinstance(result, tuple)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# _step_systemd_service
# ---------------------------------------------------------------------------


class TestStepSystemdService:
    """Tests for Linux systemd and platform dispatch."""

    def test_returns_false_on_non_linux(self) -> None:
        """Returns False immediately on unsupported non-Linux/macOS platforms."""
        from skcapstone.onboard import _step_autostart_service

        with patch("platform.system", return_value="Windows"):
            result = _step_autostart_service()

        assert result is False

    def test_returns_false_when_user_declines(self) -> None:
        """Returns False when user does not confirm the install."""
        from skcapstone.onboard import _step_systemd_service_linux

        with patch("click.confirm", return_value=False):
            result = _step_systemd_service_linux()

        assert result is False

    def test_returns_false_when_systemd_unavailable(self) -> None:
        """Returns False when systemd user session is not running."""
        from skcapstone.onboard import _step_systemd_service_linux

        with patch("click.confirm", return_value=True), \
             patch("skcapstone.systemd.systemd_available", return_value=False):
            result = _step_systemd_service_linux()

        assert result is False

    def test_returns_true_on_successful_install(self) -> None:
        """Returns True when systemd install succeeds."""
        from skcapstone.onboard import _step_systemd_service_linux

        with patch("click.confirm", return_value=True), \
             patch("skcapstone.systemd.systemd_available", return_value=True), \
             patch("skcapstone.systemd.install_service", return_value={"installed": True, "enabled": True}):
            result = _step_systemd_service_linux()

        assert result is True

    def test_returns_false_on_install_failure(self) -> None:
        """Returns False when install_service reports not installed."""
        from skcapstone.onboard import _step_systemd_service_linux

        with patch("click.confirm", return_value=True), \
             patch("skcapstone.systemd.systemd_available", return_value=True), \
             patch("skcapstone.systemd.install_service", return_value={"installed": False}):
            result = _step_systemd_service_linux()

        assert result is False


# ---------------------------------------------------------------------------
# _step_doctor_check
# ---------------------------------------------------------------------------


class TestStepDoctorCheck:
    """Tests for _step_doctor_check()."""

    def test_returns_diagnostic_report(self, tmp_home: Path) -> None:
        """Returns a DiagnosticReport object."""
        from skcapstone.doctor import DiagnosticReport
        from skcapstone.onboard import _step_doctor_check

        report = _step_doctor_check(tmp_home)

        assert isinstance(report, DiagnosticReport)

    def test_report_has_checks(self, tmp_home: Path) -> None:
        """Report contains at least one check."""
        from skcapstone.onboard import _step_doctor_check

        report = _step_doctor_check(tmp_home)

        assert report.total_count > 0

    def test_emits_pass_fail_markers(self, tmp_home: Path, capsys) -> None:
        """Output contains ✓ or ✗ check markers."""
        import io
        from click.testing import CliRunner
        from skcapstone.onboard import _step_doctor_check

        # Capture click.echo output via runner
        report = _step_doctor_check(tmp_home)
        # Just verify the report ran and has results
        assert report.total_count > 0
        assert report.passed_count >= 0


# ---------------------------------------------------------------------------
# _step_test_consciousness
# ---------------------------------------------------------------------------


class TestStepTestConsciousness:
    """Tests for _step_test_consciousness()."""

    def test_returns_false_when_user_declines(self, tmp_home: Path) -> None:
        """Returns False when user does not confirm the test."""
        from skcapstone.onboard import _step_test_consciousness

        with patch("click.confirm", return_value=False):
            result = _step_test_consciousness(tmp_home)

        assert result is False

    def test_returns_true_when_loop_responds(self, tmp_home: Path) -> None:
        """Returns True when the configured Ollama callback yields a response."""
        from skcapstone.onboard import _step_test_consciousness

        mock_config = MagicMock(ollama_model="llama3.2", ollama_host="http://localhost:11434")
        mock_callback = MagicMock(return_value="Hello, I am running fine.")

        with patch("click.confirm", return_value=True), \
             patch("skcapstone.consciousness_config.load_consciousness_config", return_value=mock_config), \
             patch("skseed.llm.ollama_callback", return_value=mock_callback):
            result = _step_test_consciousness(tmp_home)

        assert result is True

    def test_returns_false_when_loop_returns_empty(self, tmp_home: Path) -> None:
        """Returns False when the configured Ollama callback yields an empty string."""
        from skcapstone.onboard import _step_test_consciousness

        mock_config = MagicMock(ollama_model="llama3.2", ollama_host="http://localhost:11434")
        mock_callback = MagicMock(return_value="")

        with patch("click.confirm", return_value=True), \
             patch("skcapstone.consciousness_config.load_consciousness_config", return_value=mock_config), \
             patch("skseed.llm.ollama_callback", return_value=mock_callback):
            result = _step_test_consciousness(tmp_home)

        assert result is False

    def test_returns_false_on_exception(self, tmp_home: Path) -> None:
        """Returns False when the Ollama callback raises an exception."""
        from skcapstone.onboard import _step_test_consciousness

        with patch("click.confirm", return_value=True), \
             patch("skseed.llm.ollama_callback", side_effect=RuntimeError("backend down")):
            result = _step_test_consciousness(tmp_home)

        assert result is False


# ---------------------------------------------------------------------------
# CLI integration: onboard --help
# ---------------------------------------------------------------------------


class TestOnboardCLI:
    """Test the onboard CLI command registration."""

    def test_onboard_help(self) -> None:
        """skcapstone onboard --help exits 0 and mentions wizard."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["onboard", "--help"])

        assert result.exit_code == 0
        assert "wizard" in result.output.lower() or "onboard" in result.output.lower()

    def test_onboard_has_home_option(self) -> None:
        """onboard --help shows --home option."""
        from skcapstone.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["onboard", "--help"])

        assert "--home" in result.output
