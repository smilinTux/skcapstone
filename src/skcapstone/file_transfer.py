"""
Encrypted file transfer — chunked, resumable, sovereign.

Files are split into 256 KB chunks, each independently encrypted
using the agent's KMS-derived service key (Fernet AES-128-CBC).
Transfers can be paused and resumed by tracking which chunks have
been sent/received.

Architecture:
    Sender:
        1. Split file into 256 KB chunks.
        2. Encrypt each chunk with KMS service key.
        3. Write chunk files to outbox directory.
        4. Create a transfer manifest (JSON) with metadata.

    Receiver:
        1. Read manifest to learn expected chunks.
        2. Decrypt and verify each chunk (HMAC in Fernet).
        3. Reassemble in order.
        4. Verify final SHA-256 of complete file.

Storage layout:
    ~/.skcapstone/file-transfer/
    ├── outbox/
    │   └── <transfer_id>/
    │       ├── manifest.json
    │       ├── chunk-000.enc
    │       ├── chunk-001.enc
    │       └── ...
    ├── inbox/
    │   └── <transfer_id>/
    │       ├── manifest.json
    │       └── ...
    └── completed/
        └── <transfer_id>.json   # completion receipt

Usage:
    ft = FileTransfer(home)
    ft.initialize()
    transfer = ft.send("/path/to/file.pdf", recipient="lumina")
    # Receiver side:
    assembled = ft.receive(transfer_id)
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.file_transfer")

CHUNK_SIZE = 256 * 1024  # 256 KB


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ChunkInfo(BaseModel):
    """Metadata for a single file chunk."""

    index: int
    size: int
    sha256: str
    encrypted: bool = True
    sent: bool = False
    received: bool = False


class TransferManifest(BaseModel):
    """Manifest describing a complete file transfer."""

    transfer_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    filename: str
    file_size: int
    file_sha256: str
    chunk_size: int = CHUNK_SIZE
    total_chunks: int
    sender: str = ""
    recipient: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    chunks: list[ChunkInfo] = Field(default_factory=list)
    encryption_key_label: str = "file-transfer"

    @property
    def is_complete(self) -> bool:
        """Whether all chunks have been sent and received."""
        return all(c.sent for c in self.chunks) and all(c.received for c in self.chunks)

    @property
    def progress(self) -> float:
        """Transfer progress as a fraction (0.0 to 1.0)."""
        if not self.chunks:
            return 0.0
        done = sum(1 for c in self.chunks if c.sent or c.received)
        return done / len(self.chunks)


class TransferStatus(BaseModel):
    """Summary of a file transfer."""

    transfer_id: str
    filename: str
    file_size: int
    direction: str  # "send" or "receive"
    progress: float
    total_chunks: int
    chunks_done: int
    sender: str = ""
    recipient: str = ""
    created_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# FileTransfer
# ---------------------------------------------------------------------------


class FileTransfer:
    """Encrypted chunked file transfer engine.

    Splits files into 256 KB chunks, encrypts each with a KMS
    service key, and manages transfer state for resumability.

    Args:
        home: Agent home directory (~/.skcapstone).
        agent_name: Name of the local agent.
        chunk_size: Override chunk size (default 256 KB).
    """

    def __init__(
        self,
        home: Path,
        agent_name: str = "anonymous",
        chunk_size: int = CHUNK_SIZE,
    ) -> None:
        self._home = home
        self._agent = agent_name
        self._chunk_size = chunk_size
        self._base_dir = home / "file-transfer"
        self._outbox = self._base_dir / "outbox"
        self._inbox = self._base_dir / "inbox"
        self._completed = self._base_dir / "completed"

    def initialize(self) -> None:
        """Create the file transfer directory structure."""
        self._outbox.mkdir(parents=True, exist_ok=True)
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._completed.mkdir(parents=True, exist_ok=True)

    def send(
        self,
        file_path: Path,
        recipient: str,
        encrypt: bool = True,
    ) -> TransferManifest:
        """Prepare a file for transfer by chunking and encrypting.

        Args:
            file_path: Path to the file to send.
            recipient: Recipient agent name.
            encrypt: Whether to encrypt chunks (default True).

        Returns:
            TransferManifest with all chunk metadata.

        Raises:
            FileNotFoundError: If the file doesn't exist.
            ValueError: If the file is empty.
        """
        self.initialize()

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        file_data = file_path.read_bytes()
        if not file_data:
            raise ValueError("Cannot transfer empty file")

        file_hash = hashlib.sha256(file_data).hexdigest()
        total_chunks = (len(file_data) + self._chunk_size - 1) // self._chunk_size

        manifest = TransferManifest(
            filename=file_path.name,
            file_size=len(file_data),
            file_sha256=file_hash,
            chunk_size=self._chunk_size,
            total_chunks=total_chunks,
            sender=self._agent,
            recipient=recipient,
        )

        transfer_dir = self._outbox / manifest.transfer_id
        transfer_dir.mkdir(parents=True, exist_ok=True)

        enc_key = self._get_encryption_key() if encrypt else None

        for i in range(total_chunks):
            start = i * self._chunk_size
            end = min(start + self._chunk_size, len(file_data))
            chunk_data = file_data[start:end]
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()

            if encrypt and enc_key is not None:
                chunk_data = self._encrypt_chunk(chunk_data, enc_key)

            chunk_file = transfer_dir / f"chunk-{i:04d}.enc"
            chunk_file.write_bytes(chunk_data)

            manifest.chunks.append(ChunkInfo(
                index=i,
                size=end - start,
                sha256=chunk_hash,
                encrypted=encrypt and enc_key is not None,
                sent=True,
            ))

        # Write manifest
        manifest_path = transfer_dir / "manifest.json"
        manifest_path.write_text(
            manifest.model_dump_json(indent=2),
            encoding="utf-8",
        )

        logger.info(
            "Prepared transfer %s: %s (%d chunks, %d bytes) -> %s",
            manifest.transfer_id, manifest.filename,
            total_chunks, manifest.file_size, recipient,
        )

        return manifest

    def receive(
        self,
        transfer_id: str,
        output_dir: Optional[Path] = None,
    ) -> Path:
        """Receive and reassemble a file transfer.

        Reads chunks from inbox, decrypts them, verifies integrity,
        and reassembles the original file.

        Args:
            transfer_id: The transfer ID to receive.
            output_dir: Where to write the assembled file.
                Defaults to inbox/<transfer_id>/.

        Returns:
            Path to the reassembled file.

        Raises:
            FileNotFoundError: If manifest not found.
            ValueError: If integrity check fails.
        """
        # Check both inbox and outbox (for local testing)
        transfer_dir = self._inbox / transfer_id
        if not transfer_dir.is_dir():
            transfer_dir = self._outbox / transfer_id
        if not transfer_dir.is_dir():
            raise FileNotFoundError(f"Transfer {transfer_id} not found")

        manifest_path = transfer_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(f"Manifest not found for {transfer_id}")

        manifest = TransferManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )

        enc_key = self._get_encryption_key()
        assembled = bytearray()

        for chunk_info in sorted(manifest.chunks, key=lambda c: c.index):
            chunk_file = transfer_dir / f"chunk-{chunk_info.index:04d}.enc"
            if not chunk_file.exists():
                raise FileNotFoundError(
                    f"Missing chunk {chunk_info.index} for transfer {transfer_id}"
                )

            chunk_data = chunk_file.read_bytes()

            if chunk_info.encrypted and enc_key is not None:
                chunk_data = self._decrypt_chunk(chunk_data, enc_key)

            # Verify chunk integrity
            actual_hash = hashlib.sha256(chunk_data).hexdigest()
            if actual_hash != chunk_info.sha256:
                raise ValueError(
                    f"Chunk {chunk_info.index} integrity check failed: "
                    f"expected {chunk_info.sha256[:16]}..., got {actual_hash[:16]}..."
                )

            chunk_info.received = True
            assembled.extend(chunk_data)

        # Verify complete file integrity
        file_hash = hashlib.sha256(assembled).hexdigest()
        if file_hash != manifest.file_sha256:
            raise ValueError(
                f"File integrity check failed: "
                f"expected {manifest.file_sha256[:16]}..., got {file_hash[:16]}..."
            )

        # Write assembled file
        dest_dir = output_dir or transfer_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        output_path = dest_dir / manifest.filename
        output_path.write_bytes(assembled)

        # Record completion
        manifest.completed_at = datetime.now(timezone.utc)
        receipt = self._completed / f"{transfer_id}.json"
        receipt.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

        # Update manifest
        manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")

        logger.info(
            "Received transfer %s: %s (%d bytes, %d chunks)",
            transfer_id, manifest.filename, manifest.file_size, manifest.total_chunks,
        )

        return output_path

    def get_manifest(self, transfer_id: str) -> Optional[TransferManifest]:
        """Get the manifest for a transfer.

        Args:
            transfer_id: The transfer ID.

        Returns:
            TransferManifest or None if not found.
        """
        for base in (self._outbox, self._inbox):
            manifest_path = base / transfer_id / "manifest.json"
            if manifest_path.exists():
                return TransferManifest.model_validate_json(
                    manifest_path.read_text(encoding="utf-8")
                )
        return None

    def list_transfers(self, direction: Optional[str] = None) -> list[TransferStatus]:
        """List all transfers with progress info.

        Args:
            direction: Filter by "send" or "receive". None = all.

        Returns:
            List of TransferStatus objects.
        """
        statuses: list[TransferStatus] = []

        if direction in (None, "send"):
            for d in self._outbox.iterdir() if self._outbox.is_dir() else []:
                if d.is_dir():
                    manifest = self._read_manifest(d)
                    if manifest:
                        statuses.append(self._manifest_to_status(manifest, "send"))

        if direction in (None, "receive"):
            for d in self._inbox.iterdir() if self._inbox.is_dir() else []:
                if d.is_dir():
                    manifest = self._read_manifest(d)
                    if manifest:
                        statuses.append(self._manifest_to_status(manifest, "receive"))

        statuses.sort(key=lambda s: s.created_at or datetime.min, reverse=True)
        return statuses

    def resume_send(self, transfer_id: str) -> list[int]:
        """Find unsent chunks for a transfer.

        Args:
            transfer_id: The transfer ID.

        Returns:
            List of chunk indices that haven't been sent.
        """
        manifest = self.get_manifest(transfer_id)
        if manifest is None:
            return []
        return [c.index for c in manifest.chunks if not c.sent]

    def resume_receive(self, transfer_id: str) -> list[int]:
        """Find missing chunks for a transfer.

        Args:
            transfer_id: The transfer ID.

        Returns:
            List of chunk indices that haven't been received.
        """
        manifest = self.get_manifest(transfer_id)
        if manifest is None:
            return []

        transfer_dir = self._inbox / transfer_id
        if not transfer_dir.is_dir():
            transfer_dir = self._outbox / transfer_id

        missing = []
        for c in manifest.chunks:
            chunk_file = transfer_dir / f"chunk-{c.index:04d}.enc"
            if not chunk_file.exists():
                missing.append(c.index)
        return missing

    def cleanup(self, transfer_id: str) -> bool:
        """Remove all files for a completed transfer.

        Args:
            transfer_id: The transfer ID to clean up.

        Returns:
            True if cleaned up, False if not found.
        """
        cleaned = False
        for base in (self._outbox, self._inbox):
            transfer_dir = base / transfer_id
            if transfer_dir.is_dir():
                import shutil
                shutil.rmtree(transfer_dir)
                cleaned = True
        return cleaned

    def status(self) -> dict[str, Any]:
        """Return file transfer status summary."""
        outbox_count = sum(
            1 for d in self._outbox.iterdir() if d.is_dir()
        ) if self._outbox.is_dir() else 0
        inbox_count = sum(
            1 for d in self._inbox.iterdir() if d.is_dir()
        ) if self._inbox.is_dir() else 0
        completed_count = sum(
            1 for _ in self._completed.glob("*.json")
        ) if self._completed.is_dir() else 0

        return {
            "outbox_transfers": outbox_count,
            "inbox_transfers": inbox_count,
            "completed": completed_count,
            "base_dir": str(self._base_dir),
        }

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _get_encryption_key(self) -> Optional[bytes]:
        """Get the file transfer encryption key from KMS."""
        try:
            from .kms import KeyStore

            store = KeyStore(self._home)
            store.initialize()
            key_record = store.derive_service_key("file-transfer")
            return store.get_key_material(key_record.key_id)
        except Exception as exc:
            logger.warning("KMS unavailable for file transfer: %s", exc)
            return None

    def _encrypt_chunk(self, data: bytes, key: bytes) -> bytes:
        """Encrypt a chunk using Fernet."""
        from .kms import _fernet_encrypt

        return _fernet_encrypt(data, key)

    def _decrypt_chunk(self, data: bytes, key: bytes) -> bytes:
        """Decrypt a chunk using Fernet."""
        from .kms import _fernet_decrypt

        return _fernet_decrypt(data, key)

    def _read_manifest(self, transfer_dir: Path) -> Optional[TransferManifest]:
        """Read a manifest from a transfer directory."""
        manifest_path = transfer_dir / "manifest.json"
        if not manifest_path.exists():
            return None
        try:
            return TransferManifest.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None

    def _manifest_to_status(
        self, manifest: TransferManifest, direction: str,
    ) -> TransferStatus:
        """Convert a manifest to a status summary."""
        done = sum(1 for c in manifest.chunks if c.sent or c.received)
        return TransferStatus(
            transfer_id=manifest.transfer_id,
            filename=manifest.filename,
            file_size=manifest.file_size,
            direction=direction,
            progress=manifest.progress,
            total_chunks=manifest.total_chunks,
            chunks_done=done,
            sender=manifest.sender,
            recipient=manifest.recipient,
            created_at=manifest.created_at,
        )
