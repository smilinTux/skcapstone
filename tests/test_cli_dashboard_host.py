"""CLI tests for the dashboard bind-address option."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from skcapstone.cli import main


def test_dashboard_help_exposes_host() -> None:
    """Operators can discover the supported bind option."""
    result = CliRunner().invoke(main, ["dashboard", "--help"])

    assert result.exit_code == 0
    assert "--host" in result.output
    assert "127.0.0.1" in result.output


def test_dashboard_passes_host_to_server(tmp_path: Path) -> None:
    """The selected address reaches the skdashboard server factory."""
    server = MagicMock()
    with patch("skdashboard.dashboard.start_dashboard", return_value=server) as start:
        result = CliRunner().invoke(
            main,
            [
                "dashboard",
                "--home",
                str(tmp_path),
                "--host",
                "0.0.0.0",
                "--port",
                "7778",
                "--no-open",
            ],
        )

    assert result.exit_code == 0
    start.assert_called_once_with(tmp_path, host="0.0.0.0", port=7778)
    server.serve_forever.assert_called_once_with()
