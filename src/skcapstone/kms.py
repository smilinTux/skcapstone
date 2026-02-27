"""
SKSecurity KMS — Sovereign Key Management Service.

Manages cryptographic keys for agents and teams: derivation, rotation,
team membership, and audited access. Built on HKDF for derivation and
Fernet (AES-128-CBC + HMAC-SHA256) for key-at-rest encryption.

Every operation is logged to the security audit trail.

Key hierarchy:
    Agent identity key (PGP, managed by CapAuth)
    └── Master KMS key (derived via HKDF from identity fingerprint)
        ├── Service keys (per-service HKDF derivation)
        ├── Team keys (shared keys with member ACL)
        └── Subkeys (delegatable, revocable)

Storage layout:
    ~/.skcapstone/security/kms/
    ├── keystore.json          # Key metadata (KeyRecord list)
    ├── keys/                  # Encrypted key material
    │   └── <key_id>.key.enc   # Fernet-encrypted raw key bytes
    └── rotation-log.json      # Key rotation history

Usage:
    store = KeyStore(home)
    key = store.derive_service_key("api-gateway")
    team_key = store.create_team_key("dev-team", members=["opus", "lumina"])
    store.rotate_key(key.key_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.kms")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class KeyType(str, Enum):
    """Types of managed keys."""

    MASTER = "master"
    SERVICE = "service"
    TEAM = "team"
    SUBKEY = "subkey"


class KeyStatus(str, Enum):
    """Lifecycle status of a key."""

    ACTIVE = "active"
    ROTATED = "rotated"
    REVOKED = "revoked"
    EXPIRED = "expired"


class KeyRecord(BaseModel):
    """Metadata for a managed key (the actual key material is stored separately)."""

    key_id: str = Field(description="Unique key identifier (SHA-256 hash)")
    key_type: KeyType
    algorithm: str = Field(default="HKDF-SHA256+Fernet")
    label: str = Field(description="Human-readable label (e.g., 'api-gateway', 'dev-team')")
    parent_key_id: Optional[str] = Field(default=None, description="Parent key for derivations")
    fingerprint: str = Field(description="SHA-256 of the raw key material")
    status: KeyStatus = KeyStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    rotated_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    version: int = Field(default=1, description="Key version (incremented on rotation)")
    members: list[str] = Field(default_factory=list, description="Team key members (agent names)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class RotationEntry(BaseModel):
    """Audit record for a key rotation event."""

    key_id: str
    old_fingerprint: str
    new_fingerprint: str
    old_version: int
    new_version: int
    rotated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    reason: str = ""


# ---------------------------------------------------------------------------
# Cryptographic helpers
# ---------------------------------------------------------------------------

def _derive_key(master_material: bytes, info: bytes, length: int = 32) -> bytes:
    """Derive a key using HKDF-SHA256.

    Args:
        master_material: Input keying material.
        info: Context and application-specific info string.
        length: Desired output key length in bytes.

    Returns:
        Derived key bytes.
    """
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    hkdf = HKDF(
        algorithm=SHA256(),
        length=length,
        salt=None,
        info=info,
    )
    return hkdf.derive(master_material)


def _fernet_encrypt(data: bytes, key_material: bytes) -> bytes:
    """Encrypt data with Fernet (AES-128-CBC + HMAC-SHA256).

    The Fernet key is derived from the first 32 bytes of the key
    material, base64-url-encoded to meet Fernet's 32-byte requirement.

    Args:
        data: Plaintext bytes.
        key_material: At least 32 bytes of key material.

    Returns:
        Ciphertext bytes (Fernet token).
    """
    import base64
    from cryptography.fernet import Fernet

    fernet_key = base64.urlsafe_b64encode(key_material[:32])
    return Fernet(fernet_key).encrypt(data)


def _fernet_decrypt(token: bytes, key_material: bytes) -> bytes:
    """Decrypt a Fernet token.

    Args:
        token: Ciphertext bytes (Fernet token).
        key_material: Same key material used for encryption.

    Returns:
        Plaintext bytes.
    """
    import base64
    from cryptography.fernet import Fernet

    fernet_key = base64.urlsafe_b64encode(key_material[:32])
    return Fernet(fernet_key).decrypt(token)


def _key_fingerprint(raw: bytes) -> str:
    """Compute SHA-256 fingerprint of raw key material."""
    return hashlib.sha256(raw).hexdigest()


def _key_id(label: str, key_type: KeyType, version: int = 1) -> str:
    """Deterministic key ID from label + type + version."""
    data = f"skcapstone:kms:{key_type.value}:{label}:v{version}"
    return hashlib.sha256(data.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# KeyStore
# ---------------------------------------------------------------------------

class KeyStore:
    """Sovereign key management store.

    Manages the full lifecycle of cryptographic keys: derivation,
    storage, rotation, team membership, and revocation. All operations
    are audited via the security pillar.

    Args:
        home: Agent home directory (~/.skcapstone).
    """

    def __init__(self, home: Path) -> None:
        self._home = home
        self._kms_dir = home / "security" / "kms"
        self._keys_dir = self._kms_dir / "keys"
        self._keystore_file = self._kms_dir / "keystore.json"
        self._rotation_log = self._kms_dir / "rotation-log.json"
        self._master_material: Optional[bytes] = None

    def initialize(self) -> KeyRecord:
        """Initialize the KMS and derive the master key.

        The master key is derived from the agent's identity fingerprint
        via HKDF. If no identity exists, a random master is generated.

        Returns:
            KeyRecord for the master key.
        """
        self._kms_dir.mkdir(parents=True, exist_ok=True)
        self._keys_dir.mkdir(exist_ok=True)

        existing = self._load_records()
        master = next((r for r in existing if r.key_type == KeyType.MASTER), None)
        if master and master.status == KeyStatus.ACTIVE:
            self._master_material = self._load_key_material(master.key_id)
            return master

        identity_material = self._get_identity_material()
        raw_master = _derive_key(identity_material, b"skcapstone:kms:master", length=32)
        self._master_material = raw_master

        record = KeyRecord(
            key_id=_key_id("master", KeyType.MASTER),
            key_type=KeyType.MASTER,
            label="master",
            fingerprint=_key_fingerprint(raw_master),
        )

        self._save_key_material(record.key_id, raw_master)
        self._append_record(record)
        self._audit("KMS_INIT", f"KMS initialized, master key {record.key_id}")

        return record

    def derive_service_key(
        self,
        service_name: str,
        ttl_days: Optional[int] = None,
    ) -> KeyRecord:
        """Derive a service-specific key from the master.

        Args:
            service_name: Service identifier (e.g., 'api-gateway', 'skchat').
            ttl_days: Optional key expiry in days.

        Returns:
            KeyRecord for the new service key.
        """
        master = self._ensure_master()

        existing = self.get_key(service_name, KeyType.SERVICE)
        if existing and existing.status == KeyStatus.ACTIVE:
            return existing

        info = f"skcapstone:kms:service:{service_name}".encode()
        raw = _derive_key(self._master_material, info, length=32)

        expires = None
        if ttl_days:
            expires = datetime.now(timezone.utc) + timedelta(days=ttl_days)

        record = KeyRecord(
            key_id=_key_id(service_name, KeyType.SERVICE),
            key_type=KeyType.SERVICE,
            label=service_name,
            parent_key_id=master.key_id,
            fingerprint=_key_fingerprint(raw),
            expires_at=expires,
        )

        self._save_key_material(record.key_id, raw)
        self._append_record(record)
        self._audit(
            "KEY_DERIVE",
            f"Derived service key '{service_name}' ({record.key_id})",
            metadata={"key_type": "service", "label": service_name},
        )

        return record

    def derive_subkey(
        self,
        label: str,
        parent_label: Optional[str] = None,
    ) -> KeyRecord:
        """Derive a subkey for delegation.

        Args:
            label: Subkey label.
            parent_label: Parent service key label (defaults to master).

        Returns:
            KeyRecord for the new subkey.
        """
        if parent_label:
            parent = self.get_key(parent_label)
            if not parent:
                raise ValueError(f"Parent key '{parent_label}' not found")
            parent_material = self._load_key_material(parent.key_id)
            parent_id = parent.key_id
        else:
            self._ensure_master()
            parent_material = self._master_material
            parent_id = _key_id("master", KeyType.MASTER)

        info = f"skcapstone:kms:subkey:{label}".encode()
        raw = _derive_key(parent_material, info, length=32)

        record = KeyRecord(
            key_id=_key_id(label, KeyType.SUBKEY),
            key_type=KeyType.SUBKEY,
            label=label,
            parent_key_id=parent_id,
            fingerprint=_key_fingerprint(raw),
        )

        self._save_key_material(record.key_id, raw)
        self._append_record(record)
        self._audit(
            "KEY_DERIVE",
            f"Derived subkey '{label}' ({record.key_id})",
            metadata={"key_type": "subkey", "parent": parent_id},
        )

        return record

    def create_team_key(
        self,
        team_name: str,
        members: Optional[list[str]] = None,
    ) -> KeyRecord:
        """Create a shared team key.

        Team keys are random (not derived from master) so they can be
        independently rotated without affecting the key hierarchy.

        Args:
            team_name: Team identifier.
            members: Initial list of agent names with access.

        Returns:
            KeyRecord for the new team key.
        """
        self._ensure_master()

        existing = self.get_key(team_name, KeyType.TEAM)
        if existing and existing.status == KeyStatus.ACTIVE:
            return existing

        raw = secrets.token_bytes(32)

        record = KeyRecord(
            key_id=_key_id(team_name, KeyType.TEAM),
            key_type=KeyType.TEAM,
            label=team_name,
            fingerprint=_key_fingerprint(raw),
            members=members or [],
            algorithm="random+Fernet",
        )

        self._save_key_material(record.key_id, raw)
        self._append_record(record)
        self._audit(
            "TEAM_KEY_CREATE",
            f"Created team key '{team_name}' ({record.key_id}) with {len(record.members)} members",
            metadata={"team": team_name, "members": record.members},
        )

        return record

    def add_team_member(self, team_name: str, agent_name: str) -> KeyRecord:
        """Add a member to a team key's ACL.

        Args:
            team_name: Team key label.
            agent_name: Agent name to add.

        Returns:
            Updated KeyRecord.

        Raises:
            ValueError: If team key not found.
        """
        record = self.get_key(team_name, KeyType.TEAM)
        if not record:
            raise ValueError(f"Team key '{team_name}' not found")

        if agent_name in record.members:
            return record

        record.members.append(agent_name)
        self._update_record(record)
        self._audit(
            "TEAM_MEMBER_ADD",
            f"Added '{agent_name}' to team '{team_name}'",
            metadata={"team": team_name, "agent": agent_name},
        )

        return record

    def remove_team_member(self, team_name: str, agent_name: str) -> KeyRecord:
        """Remove a member from a team key's ACL.

        Args:
            team_name: Team key label.
            agent_name: Agent name to remove.

        Returns:
            Updated KeyRecord.

        Raises:
            ValueError: If team key not found.
        """
        record = self.get_key(team_name, KeyType.TEAM)
        if not record:
            raise ValueError(f"Team key '{team_name}' not found")

        if agent_name not in record.members:
            return record

        record.members.remove(agent_name)
        self._update_record(record)
        self._audit(
            "TEAM_MEMBER_REMOVE",
            f"Removed '{agent_name}' from team '{team_name}'",
            metadata={"team": team_name, "agent": agent_name},
        )

        return record

    def rotate_key(self, key_id: str, reason: str = "") -> KeyRecord:
        """Rotate a key — generate new material, increment version.

        The old key is marked ROTATED and a new active key replaces it.

        Args:
            key_id: Key to rotate.
            reason: Optional reason for the rotation.

        Returns:
            New KeyRecord for the rotated key.

        Raises:
            ValueError: If key not found.
        """
        old = self._get_record_by_id(key_id)
        if not old:
            raise ValueError(f"Key '{key_id}' not found")

        if old.key_type == KeyType.MASTER:
            return self._rotate_master(old, reason)

        old_fingerprint = old.fingerprint
        old_version = old.version

        if old.key_type == KeyType.TEAM:
            new_raw = secrets.token_bytes(32)
        else:
            self._ensure_master()
            info = f"skcapstone:kms:{old.key_type.value}:{old.label}:v{old.version + 1}".encode()
            new_raw = _derive_key(self._master_material, info, length=32)

        old.status = KeyStatus.ROTATED
        old.rotated_at = datetime.now(timezone.utc)
        self._update_record(old)

        new_record = KeyRecord(
            key_id=_key_id(old.label, old.key_type, old.version + 1),
            key_type=old.key_type,
            label=old.label,
            parent_key_id=old.parent_key_id,
            fingerprint=_key_fingerprint(new_raw),
            version=old.version + 1,
            members=old.members.copy(),
            algorithm=old.algorithm,
        )

        self._save_key_material(new_record.key_id, new_raw)
        self._append_record(new_record)

        rotation = RotationEntry(
            key_id=old.key_id,
            old_fingerprint=old_fingerprint,
            new_fingerprint=new_record.fingerprint,
            old_version=old_version,
            new_version=new_record.version,
            reason=reason,
        )
        self._append_rotation(rotation)

        self._audit(
            "KEY_ROTATE",
            f"Rotated key '{old.label}' v{old_version} -> v{new_record.version}",
            metadata={
                "key_id": old.key_id,
                "new_key_id": new_record.key_id,
                "reason": reason,
            },
        )

        return new_record

    def revoke_key(self, key_id: str, reason: str = "") -> KeyRecord:
        """Revoke a key — mark it unusable.

        Args:
            key_id: Key to revoke.
            reason: Optional reason.

        Returns:
            Updated KeyRecord.

        Raises:
            ValueError: If key not found.
        """
        record = self._get_record_by_id(key_id)
        if not record:
            raise ValueError(f"Key '{key_id}' not found")

        record.status = KeyStatus.REVOKED
        self._update_record(record)

        key_file = self._keys_dir / f"{key_id}.key.enc"
        if key_file.exists():
            key_file.unlink()

        self._audit(
            "KEY_REVOKE",
            f"Revoked key '{record.label}' ({key_id})",
            metadata={"key_id": key_id, "reason": reason},
        )

        return record

    def get_key(
        self,
        label: str,
        key_type: Optional[KeyType] = None,
    ) -> Optional[KeyRecord]:
        """Look up the latest active key by label.

        Args:
            label: Key label.
            key_type: Optional filter by key type.

        Returns:
            KeyRecord if found, None otherwise.
        """
        records = self._load_records()
        matches = [
            r for r in records
            if r.label == label and r.status == KeyStatus.ACTIVE
            and (key_type is None or r.key_type == key_type)
        ]
        if not matches:
            return None
        return max(matches, key=lambda r: r.version)

    def list_keys(
        self,
        key_type: Optional[KeyType] = None,
        include_inactive: bool = False,
    ) -> list[KeyRecord]:
        """List all managed keys.

        Args:
            key_type: Optional filter.
            include_inactive: Include rotated/revoked keys.

        Returns:
            List of KeyRecords.
        """
        records = self._load_records()
        if key_type:
            records = [r for r in records if r.key_type == key_type]
        if not include_inactive:
            records = [r for r in records if r.status == KeyStatus.ACTIVE]
        return records

    def get_key_material(self, key_id: str, agent_name: Optional[str] = None) -> bytes:
        """Retrieve raw key material (access-controlled for team keys).

        Args:
            key_id: Key to retrieve.
            agent_name: Requesting agent (checked against team ACL).

        Returns:
            Raw key bytes.

        Raises:
            ValueError: If key not found.
            PermissionError: If agent not in team ACL.
        """
        record = self._get_record_by_id(key_id)
        if not record:
            raise ValueError(f"Key '{key_id}' not found")

        if record.status != KeyStatus.ACTIVE:
            raise ValueError(f"Key '{key_id}' is {record.status.value}")

        if record.key_type == KeyType.TEAM and record.members and agent_name:
            if agent_name not in record.members:
                self._audit(
                    "KEY_ACCESS_DENIED",
                    f"Agent '{agent_name}' denied access to team key '{record.label}'",
                    metadata={"key_id": key_id, "agent": agent_name},
                )
                raise PermissionError(
                    f"Agent '{agent_name}' not in team '{record.label}' members"
                )

        material = self._load_key_material(key_id)
        self._audit(
            "KEY_ACCESS",
            f"Key material accessed: '{record.label}' ({key_id})",
            metadata={"key_id": key_id, "agent": agent_name},
        )
        return material

    def status(self) -> dict[str, Any]:
        """Return KMS status summary.

        Returns:
            Dict with key counts, health, and statistics.
        """
        records = self._load_records()
        active = [r for r in records if r.status == KeyStatus.ACTIVE]
        rotated = [r for r in records if r.status == KeyStatus.ROTATED]
        revoked = [r for r in records if r.status == KeyStatus.REVOKED]

        by_type: dict[str, int] = {}
        for r in active:
            by_type[r.key_type.value] = by_type.get(r.key_type.value, 0) + 1

        expiring_soon = [
            r for r in active
            if r.expires_at and r.expires_at < datetime.now(timezone.utc) + timedelta(days=7)
        ]

        return {
            "initialized": bool(active),
            "total_keys": len(records),
            "active": len(active),
            "rotated": len(rotated),
            "revoked": len(revoked),
            "by_type": by_type,
            "expiring_soon": [r.label for r in expiring_soon],
            "kms_dir": str(self._kms_dir),
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _ensure_master(self) -> KeyRecord:
        """Ensure the master key is loaded."""
        if self._master_material is None:
            return self.initialize()
        records = self._load_records()
        master = next((r for r in records if r.key_type == KeyType.MASTER and r.status == KeyStatus.ACTIVE), None)
        if master is None:
            return self.initialize()
        return master

    def _rotate_master(self, old: KeyRecord, reason: str) -> KeyRecord:
        """Rotate the master key (re-derives from fresh identity material)."""
        identity_material = self._get_identity_material()
        salt = secrets.token_bytes(16)
        info = f"skcapstone:kms:master:v{old.version + 1}".encode()

        from cryptography.hazmat.primitives.hashes import SHA256
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        hkdf = HKDF(algorithm=SHA256(), length=32, salt=salt, info=info)
        new_raw = hkdf.derive(identity_material)
        self._master_material = new_raw

        old.status = KeyStatus.ROTATED
        old.rotated_at = datetime.now(timezone.utc)
        self._update_record(old)

        new_record = KeyRecord(
            key_id=_key_id("master", KeyType.MASTER, old.version + 1),
            key_type=KeyType.MASTER,
            label="master",
            fingerprint=_key_fingerprint(new_raw),
            version=old.version + 1,
        )

        self._save_key_material(new_record.key_id, new_raw)
        self._append_record(new_record)

        rotation = RotationEntry(
            key_id=old.key_id,
            old_fingerprint=old.fingerprint,
            new_fingerprint=new_record.fingerprint,
            old_version=old.version,
            new_version=new_record.version,
            reason=reason,
        )
        self._append_rotation(rotation)

        self._audit(
            "MASTER_KEY_ROTATE",
            f"Master key rotated v{old.version} -> v{new_record.version}",
            metadata={"reason": reason},
        )

        return new_record

    def _get_identity_material(self) -> bytes:
        """Get identity keying material from the agent's CapAuth profile."""
        identity_file = self._home / "identity" / "identity.json"
        if identity_file.exists():
            try:
                data = json.loads(identity_file.read_text(encoding="utf-8"))
                fingerprint = data.get("fingerprint", "")
                if fingerprint:
                    return f"skcapstone:identity:{fingerprint}".encode()
            except (json.JSONDecodeError, OSError):
                pass

        logger.warning("No identity found for KMS — using random master seed")
        return secrets.token_bytes(64)

    def _load_records(self) -> list[KeyRecord]:
        """Load all key records from disk."""
        if not self._keystore_file.exists():
            return []
        try:
            data = json.loads(self._keystore_file.read_text(encoding="utf-8"))
            return [KeyRecord.model_validate(r) for r in data]
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Failed to load keystore: %s", exc)
            return []

    def _save_records(self, records: list[KeyRecord]) -> None:
        """Write all key records to disk."""
        self._kms_dir.mkdir(parents=True, exist_ok=True)
        data = [r.model_dump(mode="json") for r in records]
        self._keystore_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def _append_record(self, record: KeyRecord) -> None:
        """Add a new record to the keystore."""
        records = self._load_records()
        records.append(record)
        self._save_records(records)

    def _update_record(self, updated: KeyRecord) -> None:
        """Update an existing record in the keystore."""
        records = self._load_records()
        for i, r in enumerate(records):
            if r.key_id == updated.key_id and r.version == updated.version:
                records[i] = updated
                break
        self._save_records(records)

    def _get_record_by_id(self, key_id: str) -> Optional[KeyRecord]:
        """Find a record by key_id."""
        records = self._load_records()
        matches = [r for r in records if r.key_id == key_id]
        return matches[-1] if matches else None

    def _save_key_material(self, key_id: str, raw: bytes) -> None:
        """Encrypt and save raw key material to disk."""
        self._keys_dir.mkdir(parents=True, exist_ok=True)
        enc_key = self._get_encryption_key()
        encrypted = _fernet_encrypt(raw, enc_key)
        (self._keys_dir / f"{key_id}.key.enc").write_bytes(encrypted)

    def _load_key_material(self, key_id: str) -> bytes:
        """Load and decrypt key material from disk."""
        key_file = self._keys_dir / f"{key_id}.key.enc"
        if not key_file.exists():
            raise ValueError(f"Key material not found for '{key_id}'")
        enc_key = self._get_encryption_key()
        return _fernet_decrypt(key_file.read_bytes(), enc_key)

    def _get_encryption_key(self) -> bytes:
        """Get the encryption key for at-rest key storage.

        Uses a deterministic derivation from the agent's identity
        fingerprint so keys can be decrypted without storing a
        separate passphrase.
        """
        identity_material = self._get_identity_material()
        return _derive_key(identity_material, b"skcapstone:kms:storage-encryption", length=32)

    def _append_rotation(self, entry: RotationEntry) -> None:
        """Append a rotation event to the rotation log."""
        log: list[dict] = []
        if self._rotation_log.exists():
            try:
                log = json.loads(self._rotation_log.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        log.append(entry.model_dump(mode="json"))
        self._rotation_log.write_text(json.dumps(log, indent=2, default=str), encoding="utf-8")

    def _audit(
        self,
        event_type: str,
        detail: str,
        metadata: Optional[dict] = None,
    ) -> None:
        """Log a KMS event to the security audit trail."""
        try:
            from .pillars.security import audit_event
            audit_event(self._home, event_type, detail, agent="kms", metadata=metadata)
        except Exception:
            logger.debug("Audit log unavailable: %s — %s", event_type, detail)
