"""
Sync storage backends -- where the vault travels.

Each backend knows how to push and pull vault archives.
The engine picks which one(s) to use based on config.

Syncthing: Real-time P2P. Vault lands in sync/outbox, peers grab it.
GitHub/Forgejo: Git-based. Vault is committed and pushed.
GDrive: Google Drive API. Vault uploaded to a folder.
Local: Plain filesystem copy. For USB drives, NAS, etc.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import SyncBackendConfig, SyncBackendType

logger = logging.getLogger("skcapstone.sync.backends")


class SyncBackend(ABC):
    """Abstract sync transport backend."""

    @abstractmethod
    def push(self, vault_path: Path, manifest_path: Path) -> bool:
        """Push a vault archive to the backend.

        Args:
            vault_path: Path to the vault archive file.
            manifest_path: Path to the accompanying manifest.

        Returns:
            True if push succeeded.
        """

    @abstractmethod
    def pull(self, target_dir: Path) -> Optional[Path]:
        """Pull the latest vault from the backend.

        Args:
            target_dir: Where to download the vault.

        Returns:
            Path to the downloaded vault, or None if nothing available.
        """

    @abstractmethod
    def available(self) -> bool:
        """Check if this backend is currently usable."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable backend name."""


class SyncthingBackend(SyncBackend):
    """Syncthing-based real-time P2P sync.

    Pushes vaults to the Syncthing-watched outbox directory.
    Syncthing handles the actual peer-to-peer transfer.
    Pulls by checking the inbox for incoming vaults.
    """

    def __init__(self, config: SyncBackendConfig, agent_home: Path):
        self.config = config
        self.sync_dir = agent_home / "sync"
        self.outbox = self.sync_dir / "outbox"
        self.inbox = self.sync_dir / "inbox"
        self.archive = self.sync_dir / "archive"

        for d in (self.outbox, self.inbox, self.archive):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "syncthing"

    def push(self, vault_path: Path, manifest_path: Path) -> bool:
        """Copy vault to Syncthing outbox for P2P distribution."""
        try:
            shutil.copy2(vault_path, self.outbox / vault_path.name)
            shutil.copy2(
                manifest_path, self.outbox / manifest_path.name
            )

            state_file = self.sync_dir / "sync-state.json"
            state = {}
            if state_file.exists():
                try:
                    state = json.loads(state_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass

            state["last_push"] = datetime.now(timezone.utc).isoformat()
            state["seed_count"] = len(list(self.outbox.glob("*.tar.gz*")))
            state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

            logger.info(
                "Vault pushed to Syncthing outbox: %s", vault_path.name
            )
            return True
        except OSError as exc:
            logger.error("Syncthing push failed: %s", exc)
            return False

    def pull(self, target_dir: Path) -> Optional[Path]:
        """Check Syncthing inbox for incoming vaults."""
        vaults = sorted(
            self.inbox.glob("vault-*.tar.gz*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not vaults:
            logger.info("No vaults in Syncthing inbox")
            return None

        latest = vaults[0]
        dest = target_dir / latest.name
        shutil.copy2(latest, dest)

        shutil.move(str(latest), str(self.archive / latest.name))

        logger.info("Vault pulled from Syncthing inbox: %s", latest.name)
        return dest

    def available(self) -> bool:
        return shutil.which("syncthing") is not None


class GitBackend(SyncBackend):
    """Git-based sync backend (GitHub, Forgejo, Gitea, etc).

    Commits vault archives to a dedicated branch in a git repo.
    """

    def __init__(self, config: SyncBackendConfig, agent_home: Path):
        self.config = config
        self.agent_home = agent_home
        self._repo_dir = agent_home / "sync" / "git-cache"
        self._repo_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        backend_label = (
            "forgejo"
            if self.config.backend_type == SyncBackendType.FORGEJO
            else "github"
        )
        return backend_label

    def _ensure_repo(self) -> bool:
        """Clone or verify the git repository."""
        if not self.config.repo_url:
            logger.error("No repo_url configured for git backend")
            return False

        git_dir = self._repo_dir / ".git"
        if git_dir.exists():
            return True

        env = os.environ.copy()
        if self.config.token_env_var:
            token = os.environ.get(self.config.token_env_var, "")
            if token:
                env["GIT_ASKPASS"] = "echo"
                env["GIT_TOKEN"] = token

        result = subprocess.run(
            ["git", "clone", "--depth", "1",
             "-b", self.config.branch,
             self.config.repo_url, str(self._repo_dir)],
            capture_output=True, text=True, check=False, env=env,
        )
        return result.returncode == 0

    def push(self, vault_path: Path, manifest_path: Path) -> bool:
        if not self._ensure_repo():
            return False

        try:
            shutil.copy2(
                vault_path, self._repo_dir / vault_path.name
            )
            shutil.copy2(
                manifest_path, self._repo_dir / manifest_path.name
            )

            cmds = [
                ["git", "add", "-A"],
                [
                    "git", "commit", "-m",
                    f"vault: {vault_path.name} "
                    f"[{datetime.now(timezone.utc).isoformat()}]",
                ],
                ["git", "push", "origin", self.config.branch],
            ]
            for cmd in cmds:
                result = subprocess.run(
                    cmd, capture_output=True, text=True,
                    check=False, cwd=str(self._repo_dir),
                )
                if result.returncode != 0:
                    logger.error(
                        "Git command failed: %s -> %s",
                        " ".join(cmd), result.stderr,
                    )
                    return False

            logger.info("Vault pushed to %s", self.name)
            return True
        except OSError as exc:
            logger.error("Git push failed: %s", exc)
            return False

    def pull(self, target_dir: Path) -> Optional[Path]:
        if not self._ensure_repo():
            return None

        result = subprocess.run(
            ["git", "pull", "origin", self.config.branch],
            capture_output=True, text=True, check=False,
            cwd=str(self._repo_dir),
        )
        if result.returncode != 0:
            logger.error("Git pull failed: %s", result.stderr)
            return None

        vaults = sorted(
            self._repo_dir.glob("vault-*.tar.gz*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not vaults:
            return None

        latest = vaults[0]
        dest = target_dir / latest.name
        shutil.copy2(latest, dest)
        logger.info("Vault pulled from %s: %s", self.name, latest.name)
        return dest

    def available(self) -> bool:
        return (
            shutil.which("git") is not None
            and self.config.repo_url is not None
        )


class LocalBackend(SyncBackend):
    """Local filesystem backend for USB, NAS, or mounted drives."""

    def __init__(self, config: SyncBackendConfig, agent_home: Path):
        self.config = config
        self.target = (
            config.local_path.expanduser()
            if config.local_path
            else agent_home / "sync" / "local-backup"
        )
        self.target.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "local"

    def push(self, vault_path: Path, manifest_path: Path) -> bool:
        try:
            shutil.copy2(vault_path, self.target / vault_path.name)
            shutil.copy2(
                manifest_path, self.target / manifest_path.name
            )
            logger.info("Vault pushed to local: %s", self.target)
            return True
        except OSError as exc:
            logger.error("Local push failed: %s", exc)
            return False

    def pull(self, target_dir: Path) -> Optional[Path]:
        vaults = sorted(
            self.target.glob("vault-*.tar.gz*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not vaults:
            return None

        latest = vaults[0]
        dest = target_dir / latest.name
        shutil.copy2(latest, dest)
        logger.info("Vault pulled from local: %s", latest.name)
        return dest

    def available(self) -> bool:
        return self.target.exists()


def create_backend(
    config: SyncBackendConfig, agent_home: Path
) -> SyncBackend:
    """Factory function to create the appropriate backend.

    Args:
        config: Backend configuration.
        agent_home: Agent home directory.

    Returns:
        Instantiated SyncBackend.

    Raises:
        ValueError: If backend type is not supported.
    """
    factories = {
        SyncBackendType.SYNCTHING: SyncthingBackend,
        SyncBackendType.GITHUB: GitBackend,
        SyncBackendType.FORGEJO: GitBackend,
        SyncBackendType.LOCAL: LocalBackend,
    }
    factory = factories.get(config.backend_type)
    if not factory:
        raise ValueError(f"Unsupported backend: {config.backend_type}")
    return factory(config, agent_home)
