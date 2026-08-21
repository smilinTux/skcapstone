"""Tests for the dashboard server bind contract."""

from pathlib import Path
from unittest.mock import patch

from skdashboard.dashboard import start_dashboard


def test_start_dashboard_defaults_to_loopback(tmp_path: Path) -> None:
    """The safe default remains loopback-only."""
    with patch("skdashboard.dashboard.create_app", return_value=object()):
        server = start_dashboard(tmp_path)

    assert server._server.config.host == "127.0.0.1"


def test_start_dashboard_accepts_explicit_host(tmp_path: Path) -> None:
    """An operator can deliberately expose the listener on another interface."""
    with patch("skdashboard.dashboard.create_app", return_value=object()):
        server = start_dashboard(tmp_path, host="0.0.0.0", port=7778)

    assert server._server.config.host == "0.0.0.0"
    assert server._server.config.port == 7778


def test_start_dashboard_bounds_graceful_shutdown(tmp_path: Path) -> None:
    """Streaming clients cannot hold a systemd restart open indefinitely."""
    with patch("skdashboard.dashboard.create_app", return_value=object()):
        server = start_dashboard(tmp_path)

    assert server._server.config.timeout_graceful_shutdown == 10


def test_systemd_shutdown_policy_uses_uvicorn_signal() -> None:
    """The deployed unit must request Uvicorn's supported shutdown path."""
    policy = (
        Path(__file__).parents[1]
        / "deploy/systemd/skcapstone-dashboard.service.d/shutdown.conf"
    ).read_text(encoding="utf-8")

    assert "KillSignal=SIGINT" in policy
    assert "TimeoutStopSec=15s" in policy
