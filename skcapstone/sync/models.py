"""
Sync data models -- configuration and state for the sync system.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class SyncBackendType(str, Enum):
    """Supported sync transport backends."""

    SYNCTHING = "syncthing"
    GITHUB = "github"
    FORGEJO = "forgejo"
    GDRIVE = "gdrive"
    LOCAL = "local"


class SyncDirection(str, Enum):
    """Sync operation direction."""

    PUSH = "push"
    PULL = "pull"


class VaultManifest(BaseModel):
    """Metadata describing a vault archive.

    Travels alongside the encrypted vault so the receiver
    can verify authenticity before decryption.
    """

    agent_name: str
    source_host: str
    created_at: datetime
    schema_version: str = "1.1"
    fingerprint: Optional[str] = None
    pillars_included: list[str] = Field(default_factory=list)
    encrypted: bool = True
    signature: Optional[str] = None
    signed_by: Optional[str] = None
    archive_hash: Optional[str] = None
    file_hashes: dict[str, str] = Field(default_factory=dict)


class SyncBackendConfig(BaseModel):
    """Configuration for a single sync backend."""

    backend_type: SyncBackendType
    enabled: bool = True

    # Syncthing-specific
    syncthing_folder_id: Optional[str] = None
    syncthing_device_id: Optional[str] = None

    # Git-based (GitHub / Forgejo)
    repo_url: Optional[str] = None
    branch: str = "main"
    token_env_var: Optional[str] = None

    # Google Drive
    gdrive_folder_id: Optional[str] = None

    # Local filesystem
    local_path: Optional[Path] = None


class SyncConfig(BaseModel):
    """Complete sync configuration for an agent."""

    backends: list[SyncBackendConfig] = Field(default_factory=list)
    auto_push: bool = True
    auto_pull: bool = True
    encrypt: bool = True
    vault_path: Path = Path("vault")
    push_interval_minutes: int = 30

    # CapAuth integration
    human_fingerprint: Optional[str] = None
    agent_fingerprint: Optional[str] = None
    require_both_keys: bool = False


class SyncState(BaseModel):
    """Current sync state persisted to disk."""

    last_push: Optional[datetime] = None
    last_pull: Optional[datetime] = None
    last_push_backend: Optional[str] = None
    last_pull_backend: Optional[str] = None
    push_count: int = 0
    pull_count: int = 0
    peers_known: int = 0
    last_error: Optional[str] = None
