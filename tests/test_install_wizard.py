"""Tests for the install wizard — path selection, confirmation, routing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.install_wizard import (
    PATH_LABELS,
    _wait_for_sync,
)


class TestPathLabels:
    """Tests for path label definitions."""

    def test_all_three_paths_exist(self) -> None:
        """All three install paths have labels."""
        assert 1 in PATH_LABELS
        assert 2 in PATH_LABELS
        assert 3 in PATH_LABELS

    def test_labels_are_human_readable(self) -> None:
        """Labels contain no jargon."""
        for label in PATH_LABELS.values():
            assert len(label) > 10
            assert "sovereign singularity" not in label.lower()


class TestWaitForSync:
    """Tests for _wait_for_sync helper."""

    def test_returns_true_when_identity_exists(self, tmp_path: Path) -> None:
        """Returns True immediately if identity.json exists."""
        (tmp_path / "identity.json").write_text("{}")
        assert _wait_for_sync(tmp_path, timeout_seconds=1) is True

    def test_returns_true_when_registry_exists(self, tmp_path: Path) -> None:
        """Returns True if vault-registry.json exists."""
        (tmp_path / "vault-registry.json").write_text("{}")
        assert _wait_for_sync(tmp_path, timeout_seconds=1) is True

    def test_returns_true_when_auth_key_exists(self, tmp_path: Path) -> None:
        """Returns True if tailscale.key.gpg exists."""
        (tmp_path / "tailscale.key.gpg").write_bytes(b"encrypted")
        assert _wait_for_sync(tmp_path, timeout_seconds=1) is True

    def test_returns_false_when_empty(self, tmp_path: Path) -> None:
        """Returns False after timeout when no files exist."""
        assert _wait_for_sync(tmp_path, timeout_seconds=1) is False


class TestPathUpdateExisting:
    """Tests for path 3 — update existing node."""

    def test_exits_if_no_home(self, tmp_path: Path) -> None:
        """Path 3 exits if agent home doesn't exist."""
        from skcapstone.install_wizard import _path_update_existing

        fake_home = str(tmp_path / "nonexistent")
        with pytest.raises(SystemExit):
            _path_update_existing(
                home=fake_home,
                skip_deps=True,
                skip_ritual=True,
            )
