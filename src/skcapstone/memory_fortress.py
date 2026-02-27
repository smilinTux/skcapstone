"""
Memory Fortress — integrity sealing, at-rest encryption, and tamper alerts.

Every memory gets an HMAC-SHA256 integrity seal on write. On read, the
seal is verified and a tamper alert fires if it doesn't match. Optional
at-rest encryption uses the KMS service key derived from the agent's
master key.

Architecture:
    Fortress wraps the memory engine's _save_entry / _load_entry with:
    1. Auto-seal: HMAC-SHA256 integrity hash on every write.
    2. At-rest encryption: Fernet (AES-128-CBC + HMAC) via KMS service key.
    3. Tamper alerts: integrity verification on every read.
    4. Audit trail: every access, seal, and violation logged.

Storage layout:
    ~/.skcapstone/memory/
    ├── short-term/
    │   └── abc123def456.json     # Sealed (and optionally encrypted)
    ├── mid-term/
    ├── long-term/
    └── fortress.json             # Fortress configuration
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from .models import MemoryEntry

logger = logging.getLogger("skcapstone.memory_fortress")

# Sentinel field name inside the JSON envelope
_SEAL_FIELD = "__fortress_seal"
_ENCRYPTED_FIELD = "__fortress_encrypted"
_SEALED_AT_FIELD = "__fortress_sealed_at"
_KEY_ID_FIELD = "__fortress_key_id"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FortressConfig(BaseModel):
    """Persistent configuration for the memory fortress."""

    enabled: bool = True
    encryption_enabled: bool = False
    seal_algorithm: str = "hmac-sha256"
    kms_service_label: str = "memory-fortress"
    audit_events: bool = True


class SealResult(BaseModel):
    """Result of a seal or verify operation."""

    memory_id: str
    sealed: bool
    verified: Optional[bool] = None
    tampered: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# MemoryFortress
# ---------------------------------------------------------------------------


class MemoryFortress:
    """Integrity sealing, encryption, and tamper detection for memories.

    Wraps the memory engine's file I/O to ensure every memory is sealed
    with an HMAC-SHA256 and optionally encrypted at rest using the KMS.

    Args:
        home: Agent home directory (~/.skcapstone).
        seal_key: Optional explicit HMAC key (bytes). If not provided,
            derived from the KMS master key.
        encryption_enabled: Whether to encrypt memory content at rest.
    """

    def __init__(
        self,
        home: Path,
        seal_key: Optional[bytes] = None,
        encryption_enabled: bool = False,
    ) -> None:
        self._home = home
        self._seal_key = seal_key
        self._encryption_enabled = encryption_enabled
        self._config: Optional[FortressConfig] = None
        self._kms_key_id: Optional[str] = None

    def initialize(self) -> FortressConfig:
        """Initialize the memory fortress.

        Sets up the seal key (from KMS or explicit), loads or creates
        configuration, and ensures the KMS service key exists if
        encryption is enabled.

        Returns:
            FortressConfig with current settings.
        """
        config = self._load_config()

        if self._encryption_enabled:
            config.encryption_enabled = True

        if self._seal_key is None:
            self._seal_key = self._derive_seal_key()

        if config.encryption_enabled:
            self._ensure_encryption_key()

        self._config = config
        self._save_config(config)

        self._audit("FORTRESS_INIT", "Memory fortress initialized", metadata={
            "encryption_enabled": config.encryption_enabled,
            "seal_algorithm": config.seal_algorithm,
        })

        return config

    def seal_entry(self, entry: MemoryEntry) -> dict[str, Any]:
        """Seal a memory entry with an integrity HMAC.

        Computes HMAC-SHA256 over the memory content and metadata,
        then embeds the seal in the serialized data. If encryption
        is enabled, the content field is encrypted before sealing.

        Args:
            entry: The MemoryEntry to seal.

        Returns:
            Dict ready to be written as JSON, with fortress fields.
        """
        data = json.loads(entry.model_dump_json())
        config = self._get_config()

        if config.encryption_enabled:
            data = self._encrypt_content(data)

        seal = self._compute_seal(data)
        data[_SEAL_FIELD] = seal
        data[_SEALED_AT_FIELD] = datetime.now(timezone.utc).isoformat()

        if self._kms_key_id:
            data[_KEY_ID_FIELD] = self._kms_key_id

        if config.audit_events:
            self._audit("MEMORY_SEALED", f"Memory {entry.memory_id} sealed", metadata={
                "memory_id": entry.memory_id,
                "layer": entry.layer.value,
                "encrypted": config.encryption_enabled,
            })

        return data

    def verify_and_load(self, path: Path) -> tuple[Optional[MemoryEntry], SealResult]:
        """Load a memory file, verify its integrity seal, and decrypt.

        Args:
            path: Path to the memory JSON file.

        Returns:
            Tuple of (MemoryEntry or None, SealResult).
        """
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            return None, SealResult(
                memory_id=path.stem,
                sealed=False,
                error=f"Cannot read: {exc}",
            )

        memory_id = data.get("memory_id", path.stem)

        stored_seal = data.pop(_SEAL_FIELD, None)
        data.pop(_SEALED_AT_FIELD, None)
        data.pop(_KEY_ID_FIELD, None)

        if stored_seal is None:
            # Legacy unsealed memory — load without verification
            try:
                entry = MemoryEntry(**data)
                return entry, SealResult(
                    memory_id=memory_id,
                    sealed=False,
                    verified=None,
                )
            except Exception as exc:
                return None, SealResult(
                    memory_id=memory_id,
                    sealed=False,
                    error=str(exc),
                )

        # Verify integrity
        expected_seal = self._compute_seal(data)
        if not hmac.compare_digest(stored_seal, expected_seal):
            self._audit("MEMORY_TAMPER_ALERT", f"TAMPERED: Memory {memory_id} failed integrity check", metadata={
                "memory_id": memory_id,
                "expected_seal": expected_seal[:16] + "...",
                "actual_seal": stored_seal[:16] + "...",
                "path": str(path),
            })
            return None, SealResult(
                memory_id=memory_id,
                sealed=True,
                verified=False,
                tampered=True,
                error="Integrity seal mismatch — possible tampering",
            )

        # Decrypt if encrypted
        is_encrypted = data.pop(_ENCRYPTED_FIELD, False)
        if is_encrypted:
            data = self._decrypt_content(data)

        try:
            entry = MemoryEntry(**data)
        except Exception as exc:
            return None, SealResult(
                memory_id=memory_id,
                sealed=True,
                verified=True,
                error=f"Parse error after verification: {exc}",
            )

        config = self._get_config()
        if config.audit_events:
            self._audit("MEMORY_VERIFIED", f"Memory {memory_id} integrity verified", metadata={
                "memory_id": memory_id,
            })

        return entry, SealResult(
            memory_id=memory_id,
            sealed=True,
            verified=True,
        )

    def save_sealed(self, home: Path, entry: MemoryEntry) -> Path:
        """Seal and save a memory entry to disk.

        Atomic write using tmp + rename pattern.

        Args:
            home: Agent home directory.
            entry: The MemoryEntry to seal and save.

        Returns:
            Path where the entry was written.
        """
        from .memory_engine import _entry_path

        sealed_data = self.seal_entry(entry)
        path = _entry_path(home, entry)
        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(sealed_data, indent=2, default=str),
            encoding="utf-8",
        )
        tmp_path.rename(path)

        return path

    def verify_all(self, home: Path) -> list[SealResult]:
        """Verify integrity of all memories across all layers.

        Args:
            home: Agent home directory.

        Returns:
            List of SealResult for every memory file.
        """
        from .models import MemoryLayer

        results: list[SealResult] = []
        mem_dir = home / "memory"
        if not mem_dir.is_dir():
            return results

        tampered_count = 0
        verified_count = 0
        unsealed_count = 0

        for layer in MemoryLayer:
            layer_dir = mem_dir / layer.value
            if not layer_dir.is_dir():
                continue
            for f in sorted(layer_dir.glob("*.json")):
                _, result = self.verify_and_load(f)
                results.append(result)
                if result.tampered:
                    tampered_count += 1
                elif result.verified:
                    verified_count += 1
                elif not result.sealed:
                    unsealed_count += 1

        self._audit("FORTRESS_SCAN", "Full memory integrity scan completed", metadata={
            "total": len(results),
            "verified": verified_count,
            "tampered": tampered_count,
            "unsealed": unsealed_count,
        })

        return results

    def seal_existing(self, home: Path) -> int:
        """Seal all existing unsealed memories (migration).

        Reads each memory file, adds an integrity seal, and
        optionally encrypts if encryption is enabled.

        Args:
            home: Agent home directory.

        Returns:
            Number of memories sealed.
        """
        from .models import MemoryLayer

        sealed = 0
        mem_dir = home / "memory"
        if not mem_dir.is_dir():
            return sealed

        for layer in MemoryLayer:
            layer_dir = mem_dir / layer.value
            if not layer_dir.is_dir():
                continue
            for f in sorted(layer_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue

                if _SEAL_FIELD in data:
                    continue  # Already sealed

                try:
                    entry = MemoryEntry(**data)
                    self.save_sealed(home, entry)
                    sealed += 1
                except Exception as exc:
                    logger.warning("Cannot seal %s: %s", f.name, exc)

        if sealed:
            self._audit("FORTRESS_MIGRATION", f"Sealed {sealed} existing memories", metadata={
                "sealed_count": sealed,
            })

        return sealed

    def status(self) -> dict[str, Any]:
        """Return fortress status summary."""
        config = self._get_config()
        return {
            "enabled": config.enabled,
            "encryption_enabled": config.encryption_enabled,
            "seal_algorithm": config.seal_algorithm,
            "kms_service_label": config.kms_service_label,
            "has_seal_key": self._seal_key is not None,
            "has_encryption_key": self._kms_key_id is not None,
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _compute_seal(self, data: dict[str, Any]) -> str:
        """Compute HMAC-SHA256 over serialized memory data.

        The seal covers the entire JSON payload (excluding the seal
        field itself) to detect any modification.

        Args:
            data: Memory data dict (without the seal field).

        Returns:
            Hex-encoded HMAC-SHA256 digest.
        """
        key = self._get_seal_key()
        # Canonical JSON serialization for deterministic hashing
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
        return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

    def _get_seal_key(self) -> bytes:
        """Get the HMAC seal key, deriving from KMS if needed."""
        if self._seal_key is not None:
            return self._seal_key

        self._seal_key = self._derive_seal_key()
        return self._seal_key

    def _derive_seal_key(self) -> bytes:
        """Derive a seal key from the KMS master."""
        try:
            from .kms import KeyStore

            store = KeyStore(self._home)
            store.initialize()
            key_record = store.derive_service_key("memory-fortress-seal")
            material = store.get_key_material(key_record.key_id)
            self._kms_key_id = key_record.key_id
            return material
        except Exception as exc:
            logger.warning("KMS unavailable, using fallback seal key: %s", exc)
            # Fallback: derive from agent identity fingerprint
            return self._fallback_key()

    def _fallback_key(self) -> bytes:
        """Derive a seal key from agent identity when KMS is unavailable."""
        identity_path = self._home / "identity" / "identity.json"
        if identity_path.exists():
            try:
                data = json.loads(identity_path.read_text(encoding="utf-8"))
                fp = data.get("fingerprint", "skcapstone-default")
                return hashlib.sha256(fp.encode()).digest()
            except Exception:
                pass
        return hashlib.sha256(b"skcapstone-memory-fortress-default").digest()

    def _ensure_encryption_key(self) -> None:
        """Ensure a KMS service key exists for memory encryption."""
        try:
            from .kms import KeyStore

            store = KeyStore(self._home)
            store.initialize()
            key_record = store.derive_service_key("memory-fortress-enc")
            self._kms_key_id = key_record.key_id
        except Exception as exc:
            logger.warning("Cannot create encryption key: %s", exc)

    def _encrypt_content(self, data: dict[str, Any]) -> dict[str, Any]:
        """Encrypt the content field using KMS Fernet key."""
        try:
            from .kms import KeyStore, _fernet_encrypt

            store = KeyStore(self._home)
            store.initialize()
            key_record = store.derive_service_key("memory-fortress-enc")
            key_material = store.get_key_material(key_record.key_id)

            content = data.get("content", "")
            encrypted = _fernet_encrypt(content.encode("utf-8"), key_material)
            data["content"] = encrypted.decode("utf-8")
            data[_ENCRYPTED_FIELD] = True
        except Exception as exc:
            logger.warning("Encryption failed, storing plaintext: %s", exc)

        return data

    def _decrypt_content(self, data: dict[str, Any]) -> dict[str, Any]:
        """Decrypt the content field using KMS Fernet key."""
        try:
            from .kms import KeyStore, _fernet_decrypt

            store = KeyStore(self._home)
            store.initialize()
            key_record = store.derive_service_key("memory-fortress-enc")
            key_material = store.get_key_material(key_record.key_id)

            encrypted = data.get("content", "")
            decrypted = _fernet_decrypt(encrypted.encode("utf-8"), key_material)
            data["content"] = decrypted.decode("utf-8")
        except Exception as exc:
            logger.warning("Decryption failed: %s", exc)
            data["content"] = "[DECRYPTION FAILED]"

        return data

    def _load_config(self) -> FortressConfig:
        """Load fortress config from disk or create default."""
        config_path = self._home / "memory" / "fortress.json"
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text(encoding="utf-8"))
                return FortressConfig.model_validate(data)
            except Exception:
                pass
        return FortressConfig(encryption_enabled=self._encryption_enabled)

    def _save_config(self, config: FortressConfig) -> None:
        """Persist fortress config to disk."""
        mem_dir = self._home / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        config_path = mem_dir / "fortress.json"
        config_path.write_text(
            config.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _get_config(self) -> FortressConfig:
        """Get cached or load config."""
        if self._config is None:
            self._config = self._load_config()
        return self._config

    def _audit(self, event_type: str, detail: str, metadata: Optional[dict] = None) -> None:
        """Write an audit event if the security pillar is available."""
        try:
            from .pillars.security import audit_event

            audit_event(
                self._home,
                event_type=event_type,
                detail=detail,
                agent="memory-fortress",
                metadata=metadata,
            )
        except Exception:
            logger.debug("Audit event skipped: %s", event_type)
