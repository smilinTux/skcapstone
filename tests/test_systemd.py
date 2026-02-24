"""Tests for systemd service management module.

Tests unit file generation, install/uninstall logic, and status parsing.
Actual systemctl commands are mocked to avoid system dependencies.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.systemd import (
    ALL_UNITS,
    HEARTBEAT_SERVICE,
    HEARTBEAT_TIMER,
    QUEUE_DRAIN_SERVICE,
    QUEUE_DRAIN_TIMER,
    SERVICE_NAME,
    SOCKET_NAME,
    TIMER_UNITS,
    ServiceStatus,
    generate_unit_file,
    install_service,
    service_status,
    systemd_available,
    uninstall_service,
)


class TestGenerateUnitFile:
    """Tests for unit file generation."""

    def test_default_unit_file(self) -> None:
        """Generated unit file contains expected sections and defaults."""
        content = generate_unit_file()
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "ExecStart=skcapstone daemon start --foreground" in content
        assert "Restart=on-failure" in content
        assert "NoNewPrivileges=true" in content
        assert "WantedBy=default.target" in content

    def test_custom_python_path(self) -> None:
        """Custom Python path is used in ExecStart."""
        content = generate_unit_file(python_path="/usr/local/bin/skcapstone")
        assert "ExecStart=/usr/local/bin/skcapstone daemon start" in content

    def test_extra_env_vars(self) -> None:
        """Extra environment variables are included."""
        content = generate_unit_file(extra_env={"LOG_LEVEL": "debug", "PORT": "8888"})
        assert "Environment=LOG_LEVEL=debug" in content
        assert "Environment=PORT=8888" in content

    def test_security_hardening_present(self) -> None:
        """Security directives are in the generated unit."""
        content = generate_unit_file()
        assert "ProtectSystem=strict" in content
        assert "ProtectHome=read-only" in content
        assert "PrivateTmp=true" in content
        assert "ReadWritePaths=" in content


class TestSystemdAvailable:
    """Tests for systemd detection."""

    @patch("skcapstone.systemd._run")
    def test_available_when_systemctl_works(self, mock_run: MagicMock) -> None:
        """Returns True when systemctl --user works."""
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="systemd 256")
        assert systemd_available() is True

    @patch("skcapstone.systemd._run")
    def test_unavailable_when_systemctl_fails(self, mock_run: MagicMock) -> None:
        """Returns False when systemctl is missing."""
        mock_run.return_value = subprocess.CompletedProcess([], 1, stdout="")
        assert systemd_available() is False


class TestInstallService:
    """Tests for service installation."""

    @patch("skcapstone.systemd._systemctl")
    def test_install_copies_files(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install copies unit files to target directory."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "source"
        source.mkdir()
        (source / SERVICE_NAME).write_text("[Unit]\nDescription=Test\n")
        (source / SOCKET_NAME).write_text("[Socket]\nListenStream=127.0.0.1:7777\n")

        target = tmp_path / "target"

        result = install_service(
            unit_dir=target, source_dir=source, enable=True, start=True,
        )

        assert result["installed"] is True
        assert (target / SERVICE_NAME).exists()
        assert (target / SOCKET_NAME).exists()

    @patch("skcapstone.systemd._systemctl")
    def test_install_enables_and_starts(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install calls enable and start."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "src"
        source.mkdir()
        (source / SERVICE_NAME).write_text("[Unit]\n")

        result = install_service(
            unit_dir=tmp_path / "tgt", source_dir=source,
        )

        assert result["enabled"] is True
        assert result["started"] is True

        calls = [c.args[0] for c in mock_ctl.call_args_list]
        enable_calls = [c for c in calls if "enable" in c]
        start_calls = [c for c in calls if "start" in c]
        assert len(enable_calls) >= 1
        assert len(start_calls) >= 1

    def test_install_missing_source_returns_false(self, tmp_path: Path) -> None:
        """Install fails gracefully when source unit doesn't exist."""
        result = install_service(
            unit_dir=tmp_path / "tgt",
            source_dir=tmp_path / "nonexistent",
        )
        assert result["installed"] is False


class TestUninstallService:
    """Tests for service uninstallation."""

    @patch("skcapstone.systemd._systemctl")
    def test_uninstall_removes_files(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Uninstall removes unit files from target directory."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        (tmp_path / SERVICE_NAME).write_text("[Unit]\n")
        (tmp_path / SOCKET_NAME).write_text("[Socket]\n")

        result = uninstall_service(unit_dir=tmp_path)

        assert result["stopped"] is True
        assert result["disabled"] is True
        assert result["removed"] is True
        assert not (tmp_path / SERVICE_NAME).exists()
        assert not (tmp_path / SOCKET_NAME).exists()


class TestServiceStatus:
    """Tests for status querying."""

    def test_status_not_installed(self, tmp_path: Path) -> None:
        """Status reports not installed when unit file is missing."""
        with patch("skcapstone.systemd.SYSTEMD_USER_DIR", tmp_path / "nonexistent"):
            status = service_status()
        assert status.installed is False
        assert status.active is False

    @patch("skcapstone.systemd._systemctl")
    def test_status_running(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Status reports active when service is running."""
        (tmp_path / SERVICE_NAME).write_text("[Unit]\n")

        def side_effect(*args):
            cmd = args[0] if args else ""
            if cmd == "is-enabled":
                return subprocess.CompletedProcess([], 0, stdout="enabled\n")
            if cmd == "is-active":
                return subprocess.CompletedProcess([], 0, stdout="active\n")
            if cmd == "show":
                return subprocess.CompletedProcess(
                    [], 0,
                    stdout="MainPID=12345\nActiveEnterTimestamp=Mon 2026-02-24 05:00:00 UTC\nMemoryCurrent=52428800\nExecMainStatus=0\n",
                )
            return subprocess.CompletedProcess([], 0, stdout="")

        mock_ctl.side_effect = side_effect

        with patch("skcapstone.systemd.SYSTEMD_USER_DIR", tmp_path):
            status = service_status()

        assert status.installed is True
        assert status.enabled is True
        assert status.active is True
        assert status.pid == 12345
        assert "50.0 MB" in status.memory


class TestServiceStatusModel:
    """Tests for the ServiceStatus dataclass."""

    def test_defaults(self) -> None:
        """Default status is all-false."""
        s = ServiceStatus()
        assert s.installed is False
        assert s.enabled is False
        assert s.active is False
        assert s.pid == 0


class TestUnitConstants:
    """Tests for unit file constants and bundled files."""

    def test_all_units_includes_timers(self) -> None:
        """ALL_UNITS includes heartbeat and queue drain timers."""
        assert HEARTBEAT_TIMER in ALL_UNITS
        assert QUEUE_DRAIN_TIMER in ALL_UNITS
        assert HEARTBEAT_SERVICE in ALL_UNITS
        assert QUEUE_DRAIN_SERVICE in ALL_UNITS

    def test_timer_units_list(self) -> None:
        """TIMER_UNITS contains exactly the two timers."""
        assert len(TIMER_UNITS) == 2
        assert HEARTBEAT_TIMER in TIMER_UNITS
        assert QUEUE_DRAIN_TIMER in TIMER_UNITS

    def test_all_units_count(self) -> None:
        """ALL_UNITS has the expected number of units."""
        assert len(ALL_UNITS) == 6

    def test_bundled_service_file_exists(self) -> None:
        """The bundled skcapstone.service file exists."""
        from skcapstone.systemd import BUNDLED_DIR
        assert (BUNDLED_DIR / SERVICE_NAME).exists()

    def test_bundled_heartbeat_timer_exists(self) -> None:
        """The bundled heartbeat timer file exists."""
        from skcapstone.systemd import BUNDLED_DIR
        assert (BUNDLED_DIR / HEARTBEAT_TIMER).exists()

    def test_bundled_queue_drain_timer_exists(self) -> None:
        """The bundled queue drain timer file exists."""
        from skcapstone.systemd import BUNDLED_DIR
        assert (BUNDLED_DIR / QUEUE_DRAIN_TIMER).exists()

    def test_bundled_heartbeat_service_exists(self) -> None:
        """The bundled heartbeat service file exists."""
        from skcapstone.systemd import BUNDLED_DIR
        assert (BUNDLED_DIR / HEARTBEAT_SERVICE).exists()

    def test_bundled_queue_drain_service_exists(self) -> None:
        """The bundled queue drain service file exists."""
        from skcapstone.systemd import BUNDLED_DIR
        assert (BUNDLED_DIR / QUEUE_DRAIN_SERVICE).exists()


class TestTimerInstall:
    """Tests for timer unit installation alongside the main service."""

    @patch("skcapstone.systemd._systemctl")
    def test_install_copies_all_units(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install copies service, socket, and timer units."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "source"
        source.mkdir()
        for name in ALL_UNITS:
            (source / name).write_text(f"[Unit]\nDescription={name}\n")

        target = tmp_path / "target"
        result = install_service(unit_dir=target, source_dir=source)

        assert result["installed"] is True
        assert result["timers_enabled"] is True
        for name in ALL_UNITS:
            assert (target / name).exists(), f"{name} not copied"

    @patch("skcapstone.systemd._systemctl")
    def test_install_enables_timers(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install enables timer units."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "src"
        source.mkdir()
        for name in ALL_UNITS:
            (source / name).write_text("[Unit]\n")

        install_service(unit_dir=tmp_path / "tgt", source_dir=source)

        enable_calls = [
            c.args[0] for c in mock_ctl.call_args_list
            if len(c.args) > 0 and c.args[0] == "enable"
        ]
        assert len(enable_calls) >= 3

    @patch("skcapstone.systemd._systemctl")
    def test_uninstall_removes_all_units(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Uninstall removes all unit files including timers."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        for name in ALL_UNITS:
            (tmp_path / name).write_text("[Unit]\n")

        result = uninstall_service(unit_dir=tmp_path)

        assert result["removed"] is True
        for name in ALL_UNITS:
            assert not (tmp_path / name).exists(), f"{name} not removed"

    @patch("skcapstone.systemd._systemctl")
    def test_uninstall_stops_timers_before_service(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Uninstall stops timers before stopping the main service."""
        calls: list[tuple] = []

        def track(*args):
            calls.append(args)
            return subprocess.CompletedProcess([], 0)

        mock_ctl.side_effect = track
        (tmp_path / SERVICE_NAME).write_text("[Unit]\n")

        uninstall_service(unit_dir=tmp_path)

        stop_calls = [c[0] for c in calls if c[0] == "stop"]
        assert len(stop_calls) >= 3
