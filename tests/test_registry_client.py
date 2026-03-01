"""Tests for skcapstone.registry_client — bridge to skills-registry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from skcapstone.registry_client import RegistryClient, get_registry_client


class TestGetRegistryClient:
    """Tests for the get_registry_client() factory function."""

    def test_returns_none_when_skskills_missing(self):
        """Should return None when skskills is not installed."""
        with patch.dict("sys.modules", {"skskills.remote": None}):
            # Force ImportError by removing the module
            with patch(
                "skcapstone.registry_client.RegistryClient.__init__",
                side_effect=ImportError("no skskills"),
            ):
                result = get_registry_client()
        assert result is None

    def test_returns_client_when_skskills_available(self):
        """Should return a RegistryClient when skskills is available."""
        mock_remote = MagicMock()
        mock_module = MagicMock()
        mock_module.RemoteRegistry = mock_remote

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = get_registry_client("https://test.example.com/api")

        assert client is not None
        assert isinstance(client, RegistryClient)

    def test_custom_url_passed_to_client(self):
        """Custom URL should be forwarded to the client."""
        mock_remote = MagicMock()
        mock_module = MagicMock()
        mock_module.RemoteRegistry = mock_remote

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = get_registry_client("https://custom.example.com/api")

        assert client.registry_url == "https://custom.example.com/api"


class TestRegistryClientIsAvailable:
    """Tests for RegistryClient.is_available()."""

    def test_available_when_fetch_succeeds(self):
        """Should return True when remote responds."""
        mock_remote_instance = MagicMock()
        mock_remote_instance.fetch_index.return_value = MagicMock(skills=[])

        mock_remote_cls = MagicMock(return_value=mock_remote_instance)
        mock_module = MagicMock()
        mock_module.RemoteRegistry = mock_remote_cls

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            assert client.is_available() is True

    def test_unavailable_when_fetch_fails(self):
        """Should return False when remote is unreachable."""
        mock_remote_instance = MagicMock()
        mock_remote_instance.fetch_index.side_effect = ConnectionError("offline")

        mock_remote_cls = MagicMock(return_value=mock_remote_instance)
        mock_module = MagicMock()
        mock_module.RemoteRegistry = mock_remote_cls

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")
            assert client.is_available() is False


class TestRegistryClientListAndSearch:
    """Tests for list_skills() and search()."""

    def _make_client(self):
        """Create a client with mocked remote."""
        mock_entry_1 = MagicMock()
        mock_entry_1.model_dump.return_value = {
            "name": "syncthing-setup",
            "version": "1.0.0",
            "description": "Syncthing sovereign sync",
            "tags": ["sync"],
        }
        mock_entry_2 = MagicMock()
        mock_entry_2.model_dump.return_value = {
            "name": "pgp-identity",
            "version": "0.2.0",
            "description": "PGP key management",
            "tags": ["identity"],
        }

        mock_index = MagicMock()
        mock_index.skills = [mock_entry_1, mock_entry_2]

        mock_remote_instance = MagicMock()
        mock_remote_instance.fetch_index.return_value = mock_index
        mock_remote_instance.search.return_value = [mock_entry_1]

        mock_remote_cls = MagicMock(return_value=mock_remote_instance)
        mock_module = MagicMock()
        mock_module.RemoteRegistry = mock_remote_cls

        with patch.dict("sys.modules", {"skskills.remote": mock_module}):
            client = RegistryClient("https://test.example.com/api")

        return client

    def test_list_skills_returns_dicts(self):
        """list_skills() should return list of dicts."""
        client = self._make_client()
        skills = client.list_skills()
        assert len(skills) == 2
        assert skills[0]["name"] == "syncthing-setup"
        assert skills[1]["name"] == "pgp-identity"

    def test_search_returns_matching_dicts(self):
        """search() should return matching skill dicts."""
        client = self._make_client()
        results = client.search("syncthing")
        assert len(results) == 1
        assert results[0]["name"] == "syncthing-setup"
