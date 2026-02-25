"""Tests for the uninstall wizard — inventory, teardown, safety checks."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.uninstall_wizard import (
    _build_inventory,
    _delete_local_data,
    _dir_size,
    _human_size,
)


class TestHumanSize:
    """Tests for _human_size formatter."""

    def test_bytes(self) -> None:
        assert _human_size(512) == "512 B"

    def test_kilobytes(self) -> None:
        result = _human_size(2048)
        assert "KB" in result

    def test_megabytes(self) -> None:
        result = _human_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self) -> None:
        result = _human_size(3 * 1024 * 1024 * 1024)
        assert "GB" in result


class TestDirSize:
    """Tests for _dir_size."""

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert _dir_size(tmp_path) == 0

    def test_with_files(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world!!")
        assert _dir_size(tmp_path) == 5 + 7

    def test_nonexistent(self, tmp_path: Path) -> None:
        fake = tmp_path / "nope"
        assert _dir_size(fake) == 0


class TestBuildInventory:
    """Tests for _build_inventory."""

    def test_empty_home(self, tmp_path: Path) -> None:
        """Non-existent home returns empty inventory."""
        fake = tmp_path / "nonexistent"
        inv = _build_inventory(fake)
        assert inv["dirs"] == []
        assert inv["vault_names"] == []
        assert inv["total_size_bytes"] == 0

    def test_with_vaults(self, tmp_path: Path) -> None:
        """Detects vault directories."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        vaults = home / "vaults"
        vaults.mkdir()
        (vaults / "personal").mkdir()
        (vaults / "work").mkdir()
        (vaults / "personal" / "file.gpg").write_bytes(b"encrypted")

        inv = _build_inventory(home)
        assert "personal" in inv["vault_names"]
        assert "work" in inv["vault_names"]
        assert inv["total_size_bytes"] > 0

    def test_detects_registry(self, tmp_path: Path) -> None:
        """Detects vault-registry.json in sync folder."""
        home = tmp_path / ".skcapstone"
        sync = home / "sync"
        sync.mkdir(parents=True)
        (sync / "vault-registry.json").write_text("{}")
        inv = _build_inventory(home)
        assert inv["has_registry"] is True

    def test_detects_auth_key(self, tmp_path: Path) -> None:
        """Detects tailscale.key.gpg in sync folder."""
        home = tmp_path / ".skcapstone"
        sync = home / "sync"
        sync.mkdir(parents=True)
        (sync / "tailscale.key.gpg").write_bytes(b"encrypted")
        inv = _build_inventory(home)
        assert inv["has_auth_key"] is True


class TestDeleteLocalData:
    """Tests for _delete_local_data."""

    def test_deletes_home_dir(self, tmp_path: Path) -> None:
        """Removes the entire home directory tree."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        (home / "identity").mkdir()
        (home / "identity" / "key.asc").write_text("secret")
        (home / "memory").mkdir()
        (home / "manifest.json").write_text("{}")

        _delete_local_data(home)
        assert not home.exists()

    def test_handles_missing(self, tmp_path: Path) -> None:
        """Does not error on already-missing directory."""
        fake = tmp_path / "nonexistent"
        _delete_local_data(fake)


class TestRegistryDeregister:
    """Tests for skref registry deregister function."""

    def test_removes_device_and_vaults(self, tmp_path: Path) -> None:
        """Deregister removes device entry and its vaults."""
        from skref.registry import deregister_device, load_registry, save_registry

        registry = {
            "devices": {
                "my-desktop": {"hostname": "my-desktop", "is_datastore": True},
                "my-laptop": {"hostname": "my-laptop", "is_datastore": False},
            },
            "vaults": {
                "my-desktop:personal": {
                    "name": "personal",
                    "origin_device": "my-desktop",
                },
                "my-laptop:work": {
                    "name": "work",
                    "origin_device": "my-laptop",
                },
            },
        }
        save_registry(registry, tmp_path)

        result = deregister_device("my-desktop", sync_dir=tmp_path)
        assert result["device_removed"] is True
        assert result["vaults_removed"] == 1

        updated = load_registry(tmp_path)
        assert "my-desktop" not in updated["devices"]
        assert "my-desktop:personal" not in updated["vaults"]
        assert "my-laptop" in updated["devices"]
        assert "my-laptop:work" in updated["vaults"]

    def test_missing_device_is_safe(self, tmp_path: Path) -> None:
        """Deregistering a non-existent device doesn't error."""
        from skref.registry import deregister_device, save_registry

        save_registry({"devices": {}, "vaults": {}}, tmp_path)
        result = deregister_device("ghost", sync_dir=tmp_path)
        assert result["device_removed"] is False
        assert result["vaults_removed"] == 0


class TestTailscaleLogout:
    """Tests for tailscale logout."""

    @patch("skref.tailscale._tailscale_bin", return_value=None)
    def test_returns_false_no_binary(self, mock_bin: MagicMock) -> None:
        from skref.tailscale import logout
        assert logout() is False

    @patch("skref.tailscale._tailscale_bin", return_value="tailscale")
    @patch("skref.tailscale.subprocess.run")
    def test_logout_calls_tailscale(self, mock_run: MagicMock, mock_bin: MagicMock) -> None:
        from skref.tailscale import logout
        mock_run.return_value = MagicMock(returncode=0)
        assert logout() is True


class TestRemoveAuthKey:
    """Tests for tailscale auth key removal."""

    def test_removes_existing_key(self, tmp_path: Path) -> None:
        from skref.tailscale import remove_auth_key, AUTH_KEY_FILENAME
        key_file = tmp_path / AUTH_KEY_FILENAME
        key_file.write_bytes(b"encrypted")
        assert remove_auth_key(sync_dir=tmp_path) is True
        assert not key_file.exists()

    def test_missing_key_returns_false(self, tmp_path: Path) -> None:
        from skref.tailscale import remove_auth_key
        assert remove_auth_key(sync_dir=tmp_path) is False
