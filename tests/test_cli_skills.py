"""Tests for skcapstone skills CLI commands."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_registry_client(skills: list[dict] | None = None) -> MagicMock:
    """Return a mock RegistryClient pre-populated with skills."""
    if skills is None:
        skills = [
            {
                "name": "syncthing-setup",
                "version": "1.0.0",
                "description": "Syncthing sovereign sync",
                "tags": ["sync"],
            },
            {
                "name": "pgp-identity",
                "version": "0.2.0",
                "description": "PGP key management",
                "tags": ["identity"],
            },
        ]

    client = MagicMock()
    client.list_skills.return_value = skills
    client.search.return_value = [skills[0]] if skills else []
    return client


# ---------------------------------------------------------------------------
# skcapstone skills list
# ---------------------------------------------------------------------------


class TestSkillsList:
    """Tests for 'skcapstone skills list'."""

    def test_list_all_skills_renders_table(self):
        """Happy path: list renders a table with skill rows."""
        runner = CliRunner()
        client = _make_registry_client()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "list"])

        assert result.exit_code == 0
        assert "syncthing-setup" in result.output
        assert "pgp-identity" in result.output

    def test_list_with_query_calls_search(self):
        """--query flag should invoke client.search(), not list_skills()."""
        runner = CliRunner()
        client = _make_registry_client()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "list", "--query", "syncthing"])

        assert result.exit_code == 0
        client.search.assert_called_once_with("syncthing")
        client.list_skills.assert_not_called()

    def test_list_json_output(self):
        """--json flag should output valid JSON, not a Rich table."""
        import json

        runner = CliRunner()
        client = _make_registry_client()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "list", "--json"])

        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert isinstance(parsed, list)
        assert parsed[0]["name"] == "syncthing-setup"

    def test_list_no_skills_empty_message(self):
        """Empty registry should print a helpful 'no skills' message."""
        runner = CliRunner()
        client = _make_registry_client(skills=[])

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "list"])

        assert result.exit_code == 0
        assert "No skills found" in result.output

    def test_list_skskills_not_installed(self):
        """When get_registry_client returns None the command exits 1."""
        runner = CliRunner()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=None):
            result = runner.invoke(main, ["skills", "list"])

        assert result.exit_code == 1
        assert "skskills not installed" in result.output

    def test_list_registry_error_exits_1(self):
        """Registry connection error should print an error and exit 1."""
        runner = CliRunner()
        client = MagicMock()
        client.list_skills.side_effect = ConnectionError("offline")

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "list"])

        assert result.exit_code == 1
        assert "Registry error" in result.output


# ---------------------------------------------------------------------------
# skcapstone skills install
# ---------------------------------------------------------------------------


class TestSkillsInstall:
    """Tests for 'skcapstone skills install'."""

    def _install_result(self, name: str = "syncthing-setup") -> dict:
        return {
            "name": name,
            "version": "1.0.0",
            "agent": "global",
            "install_path": "/home/user/.skskills/installed/syncthing-setup",
            "status": "installed",
        }

    def test_install_happy_path(self):
        """Successful install prints the skill name, version, and path."""
        runner = CliRunner()
        client = MagicMock()
        client.install.return_value = self._install_result()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "install", "syncthing-setup"])

        assert result.exit_code == 0
        assert "Installed" in result.output
        assert "syncthing-setup" in result.output
        client.install.assert_called_once_with(
            "syncthing-setup", version=None, agent="global", force=False
        )

    def test_install_with_version_flag(self):
        """--version should be forwarded to client.install()."""
        runner = CliRunner()
        client = MagicMock()
        client.install.return_value = self._install_result()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(
                main, ["skills", "install", "syncthing-setup", "--version", "0.9.0"]
            )

        assert result.exit_code == 0
        client.install.assert_called_once_with(
            "syncthing-setup", version="0.9.0", agent="global", force=False
        )

    def test_install_with_agent_flag(self):
        """--agent should be forwarded to client.install()."""
        runner = CliRunner()
        client = MagicMock()
        client.install.return_value = self._install_result()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(
                main, ["skills", "install", "syncthing-setup", "--agent", "opus"]
            )

        assert result.exit_code == 0
        client.install.assert_called_once_with(
            "syncthing-setup", version=None, agent="opus", force=False
        )

    def test_install_not_found_exits_1(self):
        """FileNotFoundError from the registry should exit 1 with a helpful message."""
        runner = CliRunner()
        client = MagicMock()
        client.install.side_effect = FileNotFoundError("unknown-skill not in registry")

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(main, ["skills", "install", "unknown-skill"])

        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_install_skskills_not_installed(self):
        """When get_registry_client returns None the command exits 1."""
        runner = CliRunner()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=None):
            result = runner.invoke(main, ["skills", "install", "syncthing-setup"])

        assert result.exit_code == 1
        assert "skskills not installed" in result.output

    def test_install_force_flag(self):
        """--force should be forwarded to client.install()."""
        runner = CliRunner()
        client = MagicMock()
        client.install.return_value = self._install_result()

        with patch("skcapstone.cli.skills_cmd.get_registry_client", return_value=client):
            result = runner.invoke(
                main, ["skills", "install", "syncthing-setup", "--force"]
            )

        assert result.exit_code == 0
        client.install.assert_called_once_with(
            "syncthing-setup", version=None, agent="global", force=True
        )
