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
