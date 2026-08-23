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


def _active_directives(content: str) -> set[str]:
    """Return the set of non-comment, non-blank directive lines in a unit.

    Comment lines (starting with ``#``) are skipped so that a directive name
    mentioned inside an explanatory comment does not count as active config.
    """
    directives: set[str] = set()
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        directives.add(line)
    return directives


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
        """Generated unit carries the relaxed hardening matching the canonical
        top-level units. ProtectSystem=strict / ProtectHome=read-only are
        deliberately absent: they fail-closed when a ReadWritePaths dir is
        missing on the host, which is the known-breaking config the top-level
        units removed."""
        content = generate_unit_file()
        directives = _active_directives(content)
        assert "NoNewPrivileges=true" in directives
        assert "PrivateTmp=true" in directives
        assert "ProtectSystem=strict" not in directives
        assert "ProtectHome=read-only" not in directives

    def test_resource_caps_and_restart_backoff(self) -> None:
        """Generated unit has memory caps, restart backoff, and an alert hook."""
        content = generate_unit_file()
        # Memory caps encode the .41 host fix in the unit, not host state.
        assert "MemoryMax=4G" in content
        assert "MemoryHigh=3G" in content
        # Exponential backoff so a hard-failing daemon does not hot-loop.
        assert "RestartSteps=5" in content
        assert "RestartMaxDelaySec=300" in content
        # Crash-loop guard so a persistent failure stops within a bounded window.
        assert "StartLimitIntervalSec=1800" in content
        assert "StartLimitBurst=6" in content
        # OnFailure hook so a failed daemon pages instead of failing silently.
        assert "OnFailure=skcapstone-alert@" in content


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
        # The retired socket unit must NOT be installed even if present in source.
        (source / SOCKET_NAME).write_text("[Socket]\nListenStream=127.0.0.1:7777\n")

        target = tmp_path / "target"

        result = install_service(
            unit_dir=target,
            source_dir=source,
            enable=True,
            start=True,
        )

        assert result["installed"] is True
        assert (target / SERVICE_NAME).exists()
        # Retired (card 36d11ec3): the daemon binds its own per-agent API port.
        assert not (target / SOCKET_NAME).exists()

    @patch("skcapstone.systemd._systemctl")
    def test_install_enables_and_starts(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install calls enable and start."""
        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "src"
        source.mkdir()
        (source / SERVICE_NAME).write_text("[Unit]\n")

        result = install_service(
            unit_dir=tmp_path / "tgt",
            source_dir=source,
        )

        assert result["enabled"] is True
        assert result["started"] is True

        calls = [c.args[0] for c in mock_ctl.call_args_list]
        enable_calls = [c for c in calls if "enable" in c]
        start_calls = [c for c in calls if "start" in c]
        assert len(enable_calls) >= 1
        assert len(start_calls) >= 1

    @patch("skcapstone.systemd._systemctl")
    def test_install_copies_alert_template(self, mock_ctl: MagicMock, tmp_path: Path) -> None:
        """Install copies the OnFailure alert template so the hook resolves."""
        from skcapstone.systemd import ALERT_TEMPLATE

        mock_ctl.return_value = subprocess.CompletedProcess([], 0)

        source = tmp_path / "source"
        source.mkdir()
        (source / SERVICE_NAME).write_text("[Unit]\n")
        (source / ALERT_TEMPLATE).write_text("[Unit]\nDescription=alert\n")

        target = tmp_path / "target"
        result = install_service(unit_dir=target, source_dir=source)

        assert result["installed"] is True
        assert (target / ALERT_TEMPLATE).exists()

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
                    [],
                    0,
                    stdout="MainPID=12345\nActiveEnterTimestamp=Mon 2026-02-24 05:00:00 UTC\nMemoryCurrent=52428800\nExecMainStatus=0\n",  # noqa: E501
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
        """ALL_UNITS has the expected number of units (socket retired, card 36d11ec3)."""
        assert len(ALL_UNITS) == 5

    def test_retired_socket_not_installed(self) -> None:
        """The retired skcapstone-api.socket is no longer part of ALL_UNITS."""
        assert SOCKET_NAME not in ALL_UNITS

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

    def test_bundled_alert_template_exists(self) -> None:
        """The bundled OnFailure alert template exists."""
        from skcapstone.systemd import ALERT_TEMPLATE, BUNDLED_DIR

        assert (BUNDLED_DIR / ALERT_TEMPLATE).exists()


class TestUnitHardening:
    """Tests that the bundled agent units carry the .41 outage fixes."""

    def _read(self, name: str) -> str:
        from skcapstone.systemd import BUNDLED_DIR

        return (BUNDLED_DIR / name).read_text()

    def test_template_unit_has_memory_caps(self) -> None:
        """The per-agent template caps memory in the unit, not host state."""
        content = self._read("skcapstone@.service")
        assert "MemoryMax=4G" in content
        assert "MemoryHigh=3G" in content

    def test_template_unit_has_restart_backoff(self) -> None:
        """The per-agent template has bounded restart backoff and a start limit."""
        content = self._read("skcapstone@.service")
        assert "RestartSteps=5" in content
        assert "RestartMaxDelaySec=300" in content
        assert "StartLimitIntervalSec=1800" in content
        assert "StartLimitBurst=6" in content

    def test_template_unit_has_onfailure_hook(self) -> None:
        """The per-agent template pages via an OnFailure alert unit."""
        content = self._read("skcapstone@.service")
        assert "OnFailure=skcapstone-alert@%i.service" in content

    def test_legacy_unit_has_caps_and_backoff(self) -> None:
        """The legacy single-agent unit gets the same caps and backoff."""
        content = self._read("skcapstone.service")
        assert "MemoryMax=4G" in content
        assert "MemoryHigh=3G" in content
        assert "RestartSteps=5" in content
        assert "StartLimitIntervalSec=1800" in content
        assert "StartLimitBurst=6" in content
        assert "OnFailure=skcapstone-alert@" in content

    def test_alert_unit_is_oneshot_and_visible(self) -> None:
        """The alert unit is a oneshot that emits a visible journal event."""
        content = self._read("skcapstone-alert@.service")
        assert "Type=oneshot" in content
        assert "systemd-cat" in content


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
            c.args[0] for c in mock_ctl.call_args_list if len(c.args) > 0 and c.args[0] == "enable"
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
    def test_uninstall_stops_timers_before_service(
        self, mock_ctl: MagicMock, tmp_path: Path
    ) -> None:
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


class TestUnitTreeSingleSourceOfTruth:
    """Drift guard: the packaged unit tree must mirror the canonical one.

    There are two on-disk unit trees that MUST stay byte-identical:

      * ``systemd/`` (canonical, top-level) - deployed by scripts/install.sh
      * ``src/skcapstone/data/systemd/`` (BUNDLED_DIR) - ships in the wheel and
        is deployed by ``install_service`` on the PyPI / cold-machine path

    If they drift, a cold machine installing from PyPI gets units with the wrong
    ExecStart paths and/or known-breaking hardening. ``scripts/sync-systemd-units.py``
    regenerates the packaged tree from the canonical one; these tests fail if
    someone edits one tree without syncing the other.
    """

    _UNIT_GLOBS = ("*.service", "*.socket", "*.timer")

    def _canonical_dir(self) -> Path:
        # tests/ lives at the repo root, next to the top-level systemd/ tree.
        return Path(__file__).resolve().parent.parent / "systemd"

    def _packaged_dir(self) -> Path:
        from skcapstone.systemd import BUNDLED_DIR

        return BUNDLED_DIR

    def _units(self, directory: Path) -> dict[str, Path]:
        found: dict[str, Path] = {}
        for pattern in self._UNIT_GLOBS:
            for path in directory.glob(pattern):
                found[path.name] = path
        return found

    def test_canonical_tree_present(self) -> None:
        """The canonical top-level tree exists in a source checkout."""
        canonical = self._canonical_dir()
        if not canonical.is_dir():
            pytest.skip("canonical systemd/ tree not present (installed, not a checkout)")
        assert self._units(canonical), "canonical systemd/ tree has no unit files"

    def test_packaged_tree_matches_canonical(self) -> None:
        """Every canonical unit is present and byte-identical in the packaged tree."""
        canonical_dir = self._canonical_dir()
        if not canonical_dir.is_dir():
            pytest.skip("canonical systemd/ tree not present (installed, not a checkout)")

        canonical = self._units(canonical_dir)
        packaged = self._units(self._packaged_dir())

        missing = sorted(set(canonical) - set(packaged))
        assert not missing, (
            f"units missing from packaged tree: {missing}. "
            f"Run: python scripts/sync-systemd-units.py"
        )

        extra = sorted(set(packaged) - set(canonical))
        assert not extra, (
            f"packaged tree has units not in canonical tree: {extra}. "
            f"Run: python scripts/sync-systemd-units.py"
        )

        drifted = [
            name
            for name in sorted(canonical)
            if canonical[name].read_bytes() != packaged[name].read_bytes()
        ]
        assert not drifted, (
            f"packaged units drifted from canonical: {drifted}. "
            f"Run: python scripts/sync-systemd-units.py"
        )

    def test_packaged_agent_units_use_skenv_paths(self) -> None:
        """Packaged agent units must use %h/.skenv/bin ExecStart, not %h/.local/bin.

        A .local/bin path does not exist under the .skenv install convention, so
        a cold PyPI install would get a unit whose ExecStart binary is missing.
        """
        packaged = self._packaged_dir()
        for name in ("skcapstone.service", "skcapstone@.service"):
            content = (packaged / name).read_text()
            assert "%h/.skenv/bin/skcapstone" in content, f"{name} lost .skenv path"
            assert "%h/.local/bin/skcapstone" not in content, f"{name} has stale .local path"

    def test_packaged_agent_units_drop_breaking_hardening(self) -> None:
        """Packaged agent units must not carry the known-breaking strict hardening."""
        packaged = self._packaged_dir()
        for name in ("skcapstone.service", "skcapstone@.service"):
            directives = _active_directives((packaged / name).read_text())
            assert "ProtectSystem=strict" not in directives, f"{name} regained strict hardening"
            assert "ProtectHome=read-only" not in directives, f"{name} regained read-only home"

    def test_packaged_template_has_agent_env_block(self) -> None:
        """Packaged template must set the SKAGENT/SKCAPSTONE_AGENT/SKMEMORY_AGENT/PATH env."""
        content = (self._packaged_dir() / "skcapstone@.service").read_text()
        assert "Environment=SKAGENT=%i" in content
        assert "Environment=SKCAPSTONE_AGENT=%i" in content
        assert "Environment=SKMEMORY_AGENT=%i" in content
        assert "Environment=PATH=%h/.skenv/bin:" in content
