"""
Sync Engine -- orchestrates vault packing, encryption, and transport.

This is the command center. It reads the sync config, picks the
backend(s), manages the vault lifecycle, and coordinates push/pull.

    skcapstone sync push  ->  pack -> encrypt -> push to all backends
    skcapstone sync pull  ->  pull from backend -> verify -> decrypt -> unpack
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .backends import create_backend
from .models import (
    SyncBackendConfig,
    SyncBackendType,
    SyncConfig,
    SyncDirection,
    SyncState,
)
from .vault import Vault

logger = logging.getLogger("skcapstone.sync.engine")


class SyncEngine:
    """Orchestrates sovereign agent state synchronization.

    Manages multiple backends, vault lifecycle, encryption,
    and state tracking for push/pull operations.
    """

    def __init__(self, agent_home: Optional[Path] = None):
        """Initialize the sync engine.

        Args:
            agent_home: Path to ~/.skcapstone/. Defaults to ~/.skcapstone.
        """
        self.agent_home = (
            agent_home or Path("~/.skcapstone")
        ).expanduser()
        self.sync_dir = self.agent_home / "sync"
        self.sync_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()
        self.state = self._load_state()
        self.vault = Vault(self.agent_home)

    def _load_config(self) -> SyncConfig:
        """Load sync configuration from disk."""
        config_file = self.sync_dir / "config.yaml"
        if config_file.exists():
            try:
                data = yaml.safe_load(config_file.read_text()) or {}
                return SyncConfig(**data)
            except (yaml.YAMLError, ValueError) as exc:
                logger.warning(
                    "Failed to load sync config: %s", exc
                )
        return SyncConfig()

    def _load_state(self) -> SyncState:
        """Load sync state from disk."""
        state_file = self.sync_dir / "state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text())
                return SyncState(**data)
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "Failed to load sync state: %s", exc
                )
        return SyncState()

    def _save_state(self) -> None:
        """Persist sync state to disk."""
        state_file = self.sync_dir / "state.json"
        state_file.write_text(
            self.state.model_dump_json(indent=2)
        )

    def save_config(self) -> None:
        """Persist sync configuration to disk."""
        config_file = self.sync_dir / "config.yaml"
        data = self.config.model_dump(mode="json")
        config_file.write_text(
            yaml.dump(data, default_flow_style=False)
        )

    def add_backend(self, config: SyncBackendConfig) -> None:
        """Register a new sync backend.

        Args:
            config: Backend configuration to add.
        """
        existing = [
            b for b in self.config.backends
            if b.backend_type != config.backend_type
        ]
        existing.append(config)
        self.config.backends = existing
        self.save_config()
        logger.info(
            "Added sync backend: %s", config.backend_type.value
        )

    def push(
        self,
        passphrase: Optional[str] = None,
        pillars: Optional[list[str]] = None,
        backend_filter: Optional[str] = None,
    ) -> dict[str, bool]:
        """Push agent state to all configured backends.

        Args:
            passphrase: Encryption passphrase (for encrypted vaults).
            pillars: Which pillars to include. Defaults to all.
            backend_filter: Only push to this backend type.

        Returns:
            Dict mapping backend name to success boolean.
        """
        vault_path = self.vault.pack(
            pillars=pillars,
            encrypt=self.config.encrypt,
            passphrase=passphrase,
        )

        manifest_path = vault_path.with_suffix(".manifest.json")
        if not manifest_path.exists():
            base = vault_path.name
            if base.endswith(".gpg"):
                base = base[:-4]
            manifest_path = vault_path.parent / (
                base + ".manifest.json"
            )

        results = {}
        for bc in self.config.backends:
            if not bc.enabled:
                continue
            if (
                backend_filter
                and bc.backend_type.value != backend_filter
            ):
                continue

            backend = create_backend(bc, self.agent_home)
            if not backend.available():
                logger.warning(
                    "Backend %s not available, skipping",
                    backend.name,
                )
                results[backend.name] = False
                continue

            success = backend.push(vault_path, manifest_path)
            results[backend.name] = success

            if success:
                self.state.last_push = datetime.now(timezone.utc)
                self.state.last_push_backend = backend.name
                self.state.push_count += 1

        self._save_state()
        self._audit("SYNC_PUSH", f"Pushed to: {results}")
        return results

    def pull(
        self,
        passphrase: Optional[str] = None,
        backend_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> Optional[Path]:
        """Pull latest vault from backends and restore.

        Tries each enabled backend in order until one succeeds.

        Args:
            passphrase: Decryption passphrase.
            backend_filter: Only pull from this backend type.
            dry_run: Download but don't extract.

        Returns:
            Path to extracted state, or None if no vault found.
        """
        import tempfile

        staging = Path(tempfile.mkdtemp(prefix="skcapstone-pull-"))

        for bc in self.config.backends:
            if not bc.enabled:
                continue
            if (
                backend_filter
                and bc.backend_type.value != backend_filter
            ):
                continue

            backend = create_backend(bc, self.agent_home)
            if not backend.available():
                continue

            vault_path = backend.pull(staging)
            if vault_path is None:
                continue

            logger.info(
                "Vault received from %s: %s",
                backend.name,
                vault_path.name,
            )

            if dry_run:
                self._audit(
                    "SYNC_PULL",
                    f"Dry-run pull from {backend.name}",
                )
                return vault_path

            is_encrypted = vault_path.name.endswith(".gpg")
            result = self.vault.unpack(
                vault_path,
                decrypt=is_encrypted,
                passphrase=passphrase,
            )

            self.state.last_pull = datetime.now(timezone.utc)
            self.state.last_pull_backend = backend.name
            self.state.pull_count += 1
            self._save_state()
            self._audit(
                "SYNC_PULL",
                f"Restored from {backend.name}: {vault_path.name}",
            )
            return result

        logger.info("No vaults available from any backend")
        return None

    def status(self) -> dict:
        """Get current sync status.

        Returns:
            Dict with state, backends, and vault info.
        """
        backends_status = []
        for bc in self.config.backends:
            backend = create_backend(bc, self.agent_home)
            backends_status.append({
                "type": bc.backend_type.value,
                "enabled": bc.enabled,
                "available": backend.available(),
            })

        return {
            "state": self.state.model_dump(mode="json"),
            "backends": backends_status,
            "vaults": len(self.vault.list_vaults()),
            "encrypt": self.config.encrypt,
            "auto_push": self.config.auto_push,
        }

    def _audit(self, event_type: str, detail: str) -> None:
        """Write to the security audit log."""
        try:
            from ..pillars.security import audit_event
            audit_event(self.agent_home, event_type, detail)
        except ImportError:
            pass


def init_sync(
    agent_home: Path,
    backend_type: SyncBackendType = SyncBackendType.SYNCTHING,
    **kwargs: str,
) -> SyncEngine:
    """Initialize sync for an agent with a default backend.

    Args:
        agent_home: Agent home directory.
        backend_type: Which backend to configure initially.
        **kwargs: Backend-specific config options.

    Returns:
        Configured SyncEngine.
    """
    engine = SyncEngine(agent_home)

    config = SyncBackendConfig(backend_type=backend_type, **kwargs)
    engine.add_backend(config)

    return engine
