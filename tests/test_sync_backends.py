"""
Unit tests for skcapstone sync backends.

Covers SyncthingBackend, GitBackend, and LocalBackend with mocked
external dependencies (subprocess, shutil.which, OS errors).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / ".skcapstone"
    home.mkdir()
    for d in ("identity", "memory", "trust", "config", "skills"):
        (home / d).mkdir()
    (home / "manifest.json").write_text(
        json.dumps({"name": "TestAgent", "version": "0.1.0", "connectors": []})
    )
    return home


@pytest.fixture
def vault_files(tmp_path: Path):
    """Create a fake vault + manifest pair."""
    vault = tmp_path / "vault-host-20260228T000000Z.tar.gz"
    vault.write_bytes(b"fake tar data")
    manifest = tmp_path / "vault-host-20260228T000000Z.tar.gz.manifest.json"
    manifest.write_text(json.dumps({"agent_name": "TestAgent", "pillars_included": []}))
    return vault, manifest


# ---------------------------------------------------------------------------
# SyncthingBackend
# ---------------------------------------------------------------------------


class TestSyncthingBackend:
    def _make_backend(self, agent_home):
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        return SyncthingBackend(config, agent_home)

    def test_creates_directories(self, agent_home: Path):
        """SyncthingBackend constructor creates outbox/inbox/archive dirs."""
        backend = self._make_backend(agent_home)
        assert backend.outbox.is_dir()
        assert backend.inbox.is_dir()
        assert backend.archive.is_dir()

    def test_push_creates_state_file(self, agent_home: Path, vault_files):
        """Push should update sync-state.json with last_push timestamp."""
        vault, manifest = vault_files
        backend = self._make_backend(agent_home)
        result = backend.push(vault, manifest)

        assert result is True
        state_file = agent_home / "sync" / "sync-state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert "last_push" in state
        assert "seed_count" in state

    def test_push_updates_seed_count(self, agent_home: Path, tmp_path: Path):
        """seed_count in state should count .tar.gz files in outbox."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        # Push two vaults
        for i in range(2):
            v = tmp_path / f"vault-test-{i}.tar.gz"
            v.write_bytes(b"data")
            m = tmp_path / f"vault-test-{i}.tar.gz.manifest.json"
            m.write_text("{}")
            backend.push(v, m)

        state = json.loads((agent_home / "sync" / "sync-state.json").read_text())
        # glob "*.tar.gz*" matches both .tar.gz and .tar.gz.manifest.json files
        assert state["seed_count"] >= 2

    def test_push_handles_oserror(self, agent_home: Path, tmp_path: Path):
        """Push should return False on OSError (e.g., permissions)."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        nonexistent = tmp_path / "missing.tar.gz"
        manifest = tmp_path / "missing.manifest.json"
        # Don't create them — shutil.copy2 will raise OSError
        result = backend.push(nonexistent, manifest)
        assert result is False

    def test_push_state_file_invalid_json_recovers(self, agent_home: Path, vault_files):
        """Push should recover gracefully when existing state file is corrupt JSON."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        # Write invalid JSON to state file
        state_file = agent_home / "sync" / "sync-state.json"
        state_file.write_text("NOT JSON {{{")

        vault, manifest = vault_files
        result = backend.push(vault, manifest)
        assert result is True
        # State file should now be valid
        state = json.loads(state_file.read_text())
        assert "last_push" in state

    def test_pull_returns_latest_vault(self, agent_home: Path, tmp_path: Path):
        """Pull returns the most recently modified vault from inbox."""
        import time

        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        older = backend.inbox / "vault-peer-20260101T000000Z.tar.gz"
        older.write_bytes(b"old")
        time.sleep(0.05)
        newer = backend.inbox / "vault-peer-20260228T000000Z.tar.gz"
        newer.write_bytes(b"new")

        result = backend.pull(tmp_path)
        assert result is not None
        assert result.read_bytes() == b"new"

    def test_pull_moves_pulled_vault_to_archive(self, agent_home: Path, tmp_path: Path):
        """After pull, the source file should be moved to archive."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        inbox_file = backend.inbox / "vault-peer-20260228T000000Z.tar.gz"
        inbox_file.write_bytes(b"data")

        backend.pull(tmp_path)
        assert not inbox_file.exists()
        assert (backend.archive / "vault-peer-20260228T000000Z.tar.gz").exists()

    def test_pull_empty_inbox_returns_none(self, agent_home: Path, tmp_path: Path):
        """Pull returns None when inbox has no vault files."""
        backend = self._make_backend(agent_home)
        # Add a non-vault file to ensure glob filters correctly
        (backend.inbox / "README.txt").write_text("ignore me")
        assert backend.pull(tmp_path) is None

    def test_available_when_syncthing_in_path(self, agent_home: Path):
        """available() returns True when syncthing binary is found."""
        backend = self._make_backend(agent_home)
        with patch("shutil.which", return_value="/usr/bin/syncthing"):
            assert backend.available() is True

    def test_available_when_syncthing_missing(self, agent_home: Path):
        """available() returns False when syncthing binary is not found."""
        backend = self._make_backend(agent_home)
        with patch("shutil.which", return_value=None):
            assert backend.available() is False

    def test_name_property(self, agent_home: Path):
        backend = self._make_backend(agent_home)
        assert backend.name == "syncthing"


# ---------------------------------------------------------------------------
# GitBackend
# ---------------------------------------------------------------------------


class TestGitBackend:
    def _make_github_backend(self, agent_home: Path, repo_url: str = "https://github.com/org/repo"):
        from skcapstone.sync.backends import GitBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.GITHUB,
            repo_url=repo_url,
            branch="main",
        )
        return GitBackend(config, agent_home)

    def _make_forgejo_backend(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.FORGEJO,
            repo_url="https://forgejo.example/org/repo",
            branch="skworld",
        )
        return GitBackend(config, agent_home)

    def test_name_github(self, agent_home: Path):
        backend = self._make_github_backend(agent_home)
        assert backend.name == "github"

    def test_name_forgejo(self, agent_home: Path):
        backend = self._make_forgejo_backend(agent_home)
        assert backend.name == "forgejo"

    def test_available_requires_git_and_repo_url(self, agent_home: Path):
        backend = self._make_github_backend(agent_home)
        with patch("shutil.which", return_value="/usr/bin/git"):
            assert backend.available() is True

    def test_available_missing_git_binary(self, agent_home: Path):
        backend = self._make_github_backend(agent_home)
        with patch("shutil.which", return_value=None):
            assert backend.available() is False

    def test_available_no_repo_url(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.GITHUB, repo_url=None)
        backend = GitBackend(config, agent_home)
        with patch("shutil.which", return_value="/usr/bin/git"):
            assert backend.available() is False

    def test_ensure_repo_returns_false_if_no_repo_url(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.GITHUB, repo_url=None)
        backend = GitBackend(config, agent_home)
        assert backend._ensure_repo() is False

    def test_ensure_repo_skips_clone_if_git_dir_exists(self, agent_home: Path):
        """If .git dir already exists, _ensure_repo returns True without cloning."""
        backend = self._make_github_backend(agent_home)
        git_dir = backend._repo_dir / ".git"
        git_dir.mkdir(parents=True)

        with patch("subprocess.run") as mock_run:
            result = backend._ensure_repo()
        assert result is True
        mock_run.assert_not_called()

    def test_ensure_repo_clones_when_missing(self, agent_home: Path):
        """_ensure_repo calls git clone when .git doesn't exist."""
        backend = self._make_github_backend(agent_home)
        mock_result = MagicMock(returncode=0)
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = backend._ensure_repo()
        assert result is True
        args = mock_run.call_args[0][0]
        assert "clone" in args

    def test_ensure_repo_clone_failure_returns_false(self, agent_home: Path):
        backend = self._make_github_backend(agent_home)
        mock_result = MagicMock(returncode=1, stderr="fatal: repo not found")
        with patch("subprocess.run", return_value=mock_result):
            result = backend._ensure_repo()
        assert result is False

    def test_ensure_repo_uses_token_env_var(self, agent_home: Path):
        """_ensure_repo should set GIT_TOKEN from token_env_var in environment."""
        from skcapstone.sync.backends import GitBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.GITHUB,
            repo_url="https://github.com/org/repo",
            branch="main",
            token_env_var="MY_GIT_TOKEN",
        )
        backend = GitBackend(config, agent_home)
        mock_result = MagicMock(returncode=0)
        with patch.dict(os.environ, {"MY_GIT_TOKEN": "secret-token"}), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            backend._ensure_repo()

        env_passed = mock_run.call_args[1].get("env", {})
        assert env_passed.get("GIT_TOKEN") == "secret-token"

    def test_push_calls_git_add_commit_push(self, agent_home: Path, vault_files):
        """Push should run: git add -A, git commit, git push."""
        backend = self._make_github_backend(agent_home)
        # Pre-create .git so _ensure_repo doesn't clone
        (backend._repo_dir / ".git").mkdir(parents=True)

        mock_ok = MagicMock(returncode=0, stderr="")
        vault, manifest = vault_files
        with patch("subprocess.run", return_value=mock_ok) as mock_run:
            result = backend.push(vault, manifest)

        assert result is True
        calls = [c[0][0] for c in mock_run.call_args_list]
        git_subcommands = [c[1] for c in calls]
        assert "add" in git_subcommands
        assert "commit" in git_subcommands
        assert "push" in git_subcommands

    def test_push_returns_false_on_git_failure(self, agent_home: Path, vault_files):
        """Push should return False if any git command fails."""
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)

        vault, manifest = vault_files
        fail = MagicMock(returncode=1, stderr="fatal: error")
        ok = MagicMock(returncode=0, stderr="")
        # git add ok, git commit fails
        with patch("subprocess.run", side_effect=[ok, fail]):
            result = backend.push(vault, manifest)
        assert result is False

    def test_push_copies_vault_to_repo_dir(self, agent_home: Path, vault_files):
        """Push should copy vault and manifest into the repo dir."""
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)

        vault, manifest = vault_files
        mock_ok = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_ok):
            backend.push(vault, manifest)

        assert (backend._repo_dir / vault.name).exists()
        assert (backend._repo_dir / manifest.name).exists()

    def test_push_returns_false_if_ensure_repo_fails(self, agent_home: Path, vault_files):
        backend = self._make_github_backend(agent_home)
        vault, manifest = vault_files
        with patch.object(backend, "_ensure_repo", return_value=False):
            result = backend.push(vault, manifest)
        assert result is False

    def test_pull_calls_git_pull(self, agent_home: Path, tmp_path: Path):
        """Pull should run git pull and return the newest vault file."""
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)

        # Place a fake vault in repo dir
        fake_vault = backend._repo_dir / "vault-host-20260228T000000Z.tar.gz"
        fake_vault.write_bytes(b"vault content")

        mock_ok = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_ok):
            result = backend.pull(tmp_path)

        assert result is not None
        assert result.read_bytes() == b"vault content"

    def test_pull_returns_none_on_git_pull_failure(self, agent_home: Path, tmp_path: Path):
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)
        mock_fail = MagicMock(returncode=1, stderr="fatal")
        with patch("subprocess.run", return_value=mock_fail):
            result = backend.pull(tmp_path)
        assert result is None

    def test_pull_returns_none_when_no_vaults_in_repo(self, agent_home: Path, tmp_path: Path):
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)
        mock_ok = MagicMock(returncode=0, stderr="")
        with patch("subprocess.run", return_value=mock_ok):
            result = backend.pull(tmp_path)
        assert result is None

    def test_pull_returns_none_if_ensure_repo_fails(self, agent_home: Path, tmp_path: Path):
        backend = self._make_github_backend(agent_home)
        with patch.object(backend, "_ensure_repo", return_value=False):
            result = backend.pull(tmp_path)
        assert result is None

    def test_push_oserror_returns_false(self, agent_home: Path, tmp_path: Path):
        """Push returns False on OSError when copying files."""
        backend = self._make_github_backend(agent_home)
        (backend._repo_dir / ".git").mkdir(parents=True)

        missing_vault = tmp_path / "no-such-vault.tar.gz"
        missing_manifest = tmp_path / "no-such-vault.manifest.json"
        result = backend.push(missing_vault, missing_manifest)
        assert result is False


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------


class TestLocalBackend:
    def _make_backend(self, agent_home: Path, target: Path):
        from skcapstone.sync.backends import LocalBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=target,
        )
        return LocalBackend(config, agent_home)

    def test_name_property(self, agent_home: Path, tmp_path: Path):
        backend = self._make_backend(agent_home, tmp_path / "backup")
        assert backend.name == "local"

    def test_available_when_target_exists(self, agent_home: Path, tmp_path: Path):
        target = tmp_path / "backup"
        target.mkdir()
        backend = self._make_backend(agent_home, target)
        assert backend.available() is True

    def test_available_when_target_missing(self, agent_home: Path, tmp_path: Path):
        from skcapstone.sync.backends import LocalBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        nonexistent = tmp_path / "does-not-exist"
        config = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL, local_path=nonexistent
        )
        # Constructor creates the dir, so we need to remove it after
        backend = LocalBackend(config, agent_home)
        nonexistent.rmdir()
        assert backend.available() is False

    def test_push_copies_vault_and_manifest(self, agent_home: Path, tmp_path: Path, vault_files):
        target = tmp_path / "backup"
        target.mkdir()
        backend = self._make_backend(agent_home, target)
        vault, manifest = vault_files

        result = backend.push(vault, manifest)
        assert result is True
        assert (target / vault.name).exists()
        assert (target / manifest.name).exists()

    def test_push_returns_false_on_oserror(self, agent_home: Path, tmp_path: Path):
        target = tmp_path / "backup"
        target.mkdir()
        backend = self._make_backend(agent_home, target)

        missing = tmp_path / "missing.tar.gz"
        missing_manifest = tmp_path / "missing.manifest.json"
        result = backend.push(missing, missing_manifest)
        assert result is False

    def test_pull_returns_none_when_empty(self, agent_home: Path, tmp_path: Path):
        target = tmp_path / "backup"
        target.mkdir()
        backend = self._make_backend(agent_home, target)
        # Only put a non-vault file in target
        (target / "unrelated.txt").write_text("ignore")
        result = backend.pull(tmp_path / "dest")
        (tmp_path / "dest").mkdir(exist_ok=True)
        result = backend.pull(tmp_path / "dest")
        assert result is None

    def test_pull_returns_latest_by_mtime(self, agent_home: Path, tmp_path: Path):
        """Pull should return the most recently modified vault file."""
        import time

        target = tmp_path / "backup"
        target.mkdir()
        dest = tmp_path / "dest"
        dest.mkdir()
        backend = self._make_backend(agent_home, target)

        old = target / "vault-host-20260101T000000Z.tar.gz"
        old.write_bytes(b"old vault")
        time.sleep(0.05)
        new = target / "vault-host-20260228T000000Z.tar.gz"
        new.write_bytes(b"new vault")

        result = backend.pull(dest)
        assert result is not None
        assert result.read_bytes() == b"new vault"

    def test_default_target_when_no_local_path(self, agent_home: Path):
        """When no local_path configured, uses agent_home/sync/local-backup."""
        from skcapstone.sync.backends import LocalBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL, local_path=None
        )
        backend = LocalBackend(config, agent_home)
        expected = agent_home / "sync" / "local-backup"
        assert backend.target == expected
        assert expected.is_dir()


# ---------------------------------------------------------------------------
# create_backend factory
# ---------------------------------------------------------------------------


class TestCreateBackendFactory:
    def test_creates_gdrive_backend(self, agent_home: Path):
        from skcapstone.sync.backends import GDriveBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.GDRIVE)
        backend = create_backend(config, agent_home)
        assert isinstance(backend, GDriveBackend)
        assert backend.name == "gdrive"

    def test_syncthing_returns_syncthing_backend(self, agent_home: Path):
        from skcapstone.sync.backends import SyncthingBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        assert isinstance(create_backend(config, agent_home), SyncthingBackend)

    def test_github_returns_git_backend(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.GITHUB,
            repo_url="https://github.com/x/y",
        )
        backend = create_backend(config, agent_home)
        assert isinstance(backend, GitBackend)
        assert backend.name == "github"

    def test_forgejo_returns_git_backend(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.FORGEJO,
            repo_url="https://forgejo.example/x/y",
        )
        backend = create_backend(config, agent_home)
        assert isinstance(backend, GitBackend)
        assert backend.name == "forgejo"

    def test_local_returns_local_backend(self, agent_home: Path, tmp_path: Path):
        from skcapstone.sync.backends import LocalBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=tmp_path / "bkp",
        )
        assert isinstance(create_backend(config, agent_home), LocalBackend)
