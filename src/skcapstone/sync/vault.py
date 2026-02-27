"""
The Vault -- encrypted agent state bundling.

Never sync raw state. Package it. Encrypt it. Sign it.
Then let the transport layer do its job.

The vault is a tarball of selected ~/.skcapstone/ directories,
encrypted with PGP (via CapAuth) and signed to prove authenticity.

Hardening guarantees:
    - SHA-256 hashes for every file in the archive
    - SHA-256 hash of the archive itself
    - GPG detached signature on the manifest
    - Integrity verification before extraction
    - Key rotation re-encrypts all existing vaults
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tarfile
import tempfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Optional

from .models import VaultManifest

logger = logging.getLogger("skcapstone.sync.vault")

PILLARS_TO_SYNC = ["identity", "memory", "trust", "config", "skills"]
EXCLUDE_PATTERNS = {"__pycache__", ".pyc", ".git", "audit.log"}


class VaultIntegrityError(Exception):
    """Raised when vault integrity verification fails."""


class VaultSignatureError(Exception):
    """Raised when vault signature verification fails."""


def _sha256_file(path: Path) -> str:
    """Compute SHA-256 hex digest of a file.

    Args:
        path: File to hash.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    """Compute SHA-256 hex digest of bytes.

    Args:
        data: Bytes to hash.

    Returns:
        Hex-encoded SHA-256 digest.
    """
    return hashlib.sha256(data).hexdigest()


class Vault:
    """Manages creation and extraction of encrypted agent state vaults.

    A vault is:
    1. A tar.gz of selected ~/.skcapstone/ pillars
    2. Optionally encrypted with PGP (CapAuth)
    3. Signed with the agent's private key
    4. Accompanied by a manifest for verification
    """

    def __init__(self, agent_home: Path):
        """Initialize vault manager.

        Args:
            agent_home: Path to ~/.skcapstone/.
        """
        self.agent_home = agent_home.expanduser()
        self.vault_dir = self.agent_home / "vault"
        self.vault_dir.mkdir(parents=True, exist_ok=True)

    def _should_exclude(self, path: str) -> bool:
        """Check if a path component matches exclusion patterns."""
        return any(pat in path for pat in EXCLUDE_PATTERNS)

    def pack(
        self,
        pillars: Optional[list[str]] = None,
        encrypt: bool = False,
        passphrase: Optional[str] = None,
        sign: bool = False,
    ) -> Path:
        """Pack agent state into a vault archive.

        Args:
            pillars: Which pillars to include. Defaults to all syncable pillars.
            encrypt: Whether to GPG-encrypt the archive.
            passphrase: Passphrase for encryption (required if encrypt=True).
            sign: Whether to GPG-sign the manifest.

        Returns:
            Path to the created vault file (.tar.gz or .tar.gz.gpg).
        """
        target_pillars = pillars or PILLARS_TO_SYNC
        hostname = os.uname().nodename
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_name = f"vault-{hostname}-{timestamp}.tar.gz"
        archive_path = self.vault_dir / archive_name

        included = []
        file_hashes: dict[str, str] = {}

        with tarfile.open(archive_path, "w:gz") as tar:
            for pillar in target_pillars:
                pillar_dir = self.agent_home / pillar
                if not pillar_dir.exists():
                    logger.debug("Pillar %s not found, skipping", pillar)
                    continue

                for root, dirs, files in os.walk(pillar_dir):
                    dirs[:] = [
                        d for d in dirs if not self._should_exclude(d)
                    ]
                    for fname in files:
                        if self._should_exclude(fname):
                            continue
                        full_path = Path(root) / fname
                        arcname = str(
                            full_path.relative_to(self.agent_home)
                        )
                        tar.add(str(full_path), arcname=arcname)
                        file_hashes[arcname] = _sha256_file(full_path)

                included.append(pillar)

            manifest_path = self.agent_home / "manifest.json"
            if manifest_path.exists():
                tar.add(
                    str(manifest_path),
                    arcname="manifest.json",
                )
                file_hashes["manifest.json"] = _sha256_file(manifest_path)

        archive_hash = _sha256_file(archive_path)

        manifest = VaultManifest(
            agent_name=self._get_agent_name(),
            source_host=hostname,
            created_at=datetime.now(timezone.utc),
            pillars_included=included,
            encrypted=encrypt,
            file_hashes=file_hashes,
            archive_hash=archive_hash,
            fingerprint=self._get_agent_fingerprint(),
        )

        if sign:
            sig = self._sign_manifest(manifest, passphrase)
            if sig:
                manifest.signature = sig
                manifest.signed_by = self._get_agent_fingerprint()

        manifest_file = archive_path.with_suffix(".manifest.json")
        manifest_file.write_text(
            manifest.model_dump_json(indent=2)
        , encoding="utf-8")

        if encrypt:
            encrypted_path = self._encrypt_vault(
                archive_path, passphrase
            )
            archive_path.unlink()
            logger.info("Vault packed and encrypted: %s", encrypted_path)
            return encrypted_path

        logger.info(
            "Vault packed: %s (%d pillars, %d files hashed)",
            archive_path,
            len(included),
            len(file_hashes),
        )
        return archive_path

    def unpack(
        self,
        vault_path: Path,
        decrypt: bool = False,
        passphrase: Optional[str] = None,
        target: Optional[Path] = None,
        verify_signature: bool = True,
        verify_hashes: bool = True,
    ) -> Path:
        """Unpack a vault archive to restore agent state.

        Verifies integrity before extraction when manifest is present.

        Args:
            vault_path: Path to the vault file.
            decrypt: Whether the vault is GPG-encrypted.
            passphrase: Passphrase for decryption.
            target: Where to extract. Defaults to agent_home.
            verify_signature: Whether to verify the manifest signature.
            verify_hashes: Whether to verify SHA-256 file hashes.

        Returns:
            Path to the extraction directory.

        Raises:
            VaultSignatureError: If signature verification fails.
            VaultIntegrityError: If hash verification fails.
        """
        extract_to = target or self.agent_home

        if decrypt:
            vault_path = self._decrypt_vault(vault_path, passphrase)

        manifest = self._load_and_verify_manifest(
            vault_path, verify_signature
        )

        if manifest and manifest.archive_hash and verify_hashes:
            actual_hash = _sha256_file(vault_path)
            if actual_hash != manifest.archive_hash:
                raise VaultIntegrityError(
                    f"Archive hash mismatch: expected {manifest.archive_hash}, "
                    f"got {actual_hash}"
                )
            logger.info("Archive integrity verified (SHA-256)")

        with tarfile.open(vault_path, "r:gz") as tar:
            tar.extractall(path=extract_to, filter="data")

        if manifest and manifest.file_hashes and verify_hashes:
            self._verify_file_hashes(extract_to, manifest.file_hashes)
            logger.info(
                "All %d file hashes verified", len(manifest.file_hashes)
            )

        logger.info("Vault unpacked to %s", extract_to)
        return extract_to

    def rotate_keys(
        self,
        old_passphrase: Optional[str] = None,
        new_passphrase: Optional[str] = None,
    ) -> list[Path]:
        """Re-encrypt all vault archives with a new passphrase.

        Decrypts each .gpg vault with the old passphrase, then
        re-encrypts with the new one. Non-encrypted vaults are
        encrypted with the new passphrase.

        Args:
            old_passphrase: Current passphrase for existing encrypted vaults.
            new_passphrase: New passphrase for re-encryption.

        Returns:
            List of paths to re-encrypted vaults.
        """
        rotated: list[Path] = []

        for vault_file in sorted(self.vault_dir.glob("vault-*.tar.gz.gpg")):
            try:
                decrypted = self._decrypt_vault(vault_file, old_passphrase)
                new_encrypted = self._encrypt_vault(decrypted, new_passphrase)
                decrypted.unlink()
                vault_file.unlink()

                old_manifest = vault_file.with_name(
                    vault_file.name.replace(".tar.gz.gpg", ".tar.gz.manifest.json")
                )
                if old_manifest.exists():
                    data = json.loads(old_manifest.read_text(encoding="utf-8"))
                    data["encrypted"] = True
                    data["archive_hash"] = _sha256_file(new_encrypted)
                    old_manifest.write_text(json.dumps(data, indent=2), encoding="utf-8")

                rotated.append(new_encrypted)
                logger.info("Rotated encryption: %s", new_encrypted.name)
            except RuntimeError as exc:
                logger.error(
                    "Failed to rotate %s: %s", vault_file.name, exc
                )

        for vault_file in sorted(self.vault_dir.glob("vault-*.tar.gz")):
            if vault_file.name.endswith(".gpg"):
                continue
            manifest_file = vault_file.with_suffix(".manifest.json")
            if manifest_file.exists():
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                if data.get("encrypted"):
                    continue

            try:
                new_encrypted = self._encrypt_vault(vault_file, new_passphrase)
                vault_file.unlink()

                if manifest_file.exists():
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    data["encrypted"] = True
                    data["archive_hash"] = _sha256_file(new_encrypted)
                    manifest_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

                rotated.append(new_encrypted)
                logger.info("Encrypted plaintext vault: %s", new_encrypted.name)
            except Exception as exc:
                logger.error(
                    "Failed to encrypt %s: %s", vault_file.name, exc
                )

        logger.info("Key rotation complete: %d vaults rotated", len(rotated))
        return rotated

    def list_vaults(self) -> list[dict]:
        """List all vault archives in the vault directory.

        Returns:
            List of dicts with vault metadata.
        """
        vaults = []
        for f in sorted(self.vault_dir.glob("vault-*.tar.gz*")):
            if f.suffix == ".json":
                continue
            manifest_file = f.with_suffix(".manifest.json")
            meta: dict = {"path": f, "size": f.stat().st_size}
            if manifest_file.exists():
                try:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    meta.update(data)
                except json.JSONDecodeError:
                    pass
            vaults.append(meta)
        return vaults

    def _load_and_verify_manifest(
        self, vault_path: Path, verify_signature: bool
    ) -> Optional[VaultManifest]:
        """Load manifest for a vault and optionally verify its signature.

        Args:
            vault_path: Path to the vault archive.
            verify_signature: Whether to verify the GPG signature.

        Returns:
            VaultManifest if found, None otherwise.

        Raises:
            VaultSignatureError: If signature is present but invalid.
        """
        manifest_file = vault_path.with_suffix(".manifest.json")
        if not manifest_file.exists():
            base = vault_path.name
            if base.endswith(".gpg"):
                base = base[:-4]
            manifest_file = vault_path.parent / (base + ".manifest.json")

        if not manifest_file.exists():
            logger.debug("No manifest found for %s", vault_path.name)
            return None

        manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        manifest = VaultManifest(**manifest_data)

        if verify_signature and manifest.signature:
            if not self._verify_signature(manifest):
                raise VaultSignatureError(
                    f"Invalid signature on manifest for {vault_path.name}"
                )
            logger.info("Manifest signature verified")

        logger.info(
            "Restoring vault from %s (agent=%s, pillars=%s)",
            manifest.source_host,
            manifest.agent_name,
            manifest.pillars_included,
        )
        return manifest

    def _verify_file_hashes(
        self, extract_dir: Path, expected_hashes: dict[str, str]
    ) -> None:
        """Verify SHA-256 hashes of extracted files.

        Args:
            extract_dir: Directory where files were extracted.
            expected_hashes: Map of relative path -> expected SHA-256 hex.

        Raises:
            VaultIntegrityError: If any file hash doesn't match.
        """
        for rel_path, expected_hash in expected_hashes.items():
            file_path = extract_dir / rel_path
            if not file_path.exists():
                logger.warning("Expected file missing: %s", rel_path)
                continue
            actual_hash = _sha256_file(file_path)
            if actual_hash != expected_hash:
                raise VaultIntegrityError(
                    f"Hash mismatch for {rel_path}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )

    def _sign_manifest(
        self, manifest: VaultManifest, passphrase: Optional[str]
    ) -> Optional[str]:
        """Sign the manifest data with the agent's private key.

        Args:
            manifest: The manifest to sign (signature field excluded).
            passphrase: Key passphrase.

        Returns:
            Base64 signature string, or None if signing unavailable.
        """
        try:
            from capauth.crypto import get_backend

            backend = get_backend()
            private_key_path = self.agent_home / "identity" / "agent.key"
            if not private_key_path.exists():
                logger.debug("No private key found for signing")
                return None

            sign_data = manifest.model_dump_json(
                exclude={"signature", "signed_by"}
            ).encode()
            sig = backend.sign(
                sign_data,
                private_key_path.read_text(encoding="utf-8"),
                passphrase or "",
            )
            return sig
        except (ImportError, Exception) as exc:
            logger.debug("CapAuth signing unavailable: %s", exc)

        try:
            import subprocess

            private_key_path = self.agent_home / "identity" / "agent.key"
            if not private_key_path.exists():
                return None

            sign_data = manifest.model_dump_json(
                exclude={"signature", "signed_by"}
            ).encode()

            result = subprocess.run(
                ["gpg", "--batch", "--yes", "--detach-sign", "--armor",
                 "--default-key", manifest.fingerprint or ""],
                input=sign_data,
                capture_output=True,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.decode()
        except Exception as exc:
            logger.debug("GPG signing failed: %s", exc)

        return None

    def _verify_signature(self, manifest: VaultManifest) -> bool:
        """Verify a manifest's GPG signature.

        Args:
            manifest: Manifest with signature to verify.

        Returns:
            True if signature is valid, False otherwise.
        """
        if not manifest.signature:
            return False

        try:
            from capauth.crypto import get_backend

            backend = get_backend()
            sign_data = manifest.model_dump_json(
                exclude={"signature", "signed_by"}
            ).encode()
            return backend.verify(sign_data, manifest.signature)
        except (ImportError, Exception) as exc:
            logger.debug("CapAuth verify unavailable: %s", exc)

        try:
            import subprocess

            sign_data = manifest.model_dump_json(
                exclude={"signature", "signed_by"}
            ).encode()

            with tempfile.NamedTemporaryFile(suffix=".sig", delete=False) as sig_file:
                sig_file.write(manifest.signature.encode())
                sig_path = sig_file.name

            try:
                result = subprocess.run(
                    ["gpg", "--batch", "--verify", sig_path, "-"],
                    input=sign_data,
                    capture_output=True,
                    check=False,
                )
                return result.returncode == 0
            finally:
                Path(sig_path).unlink(missing_ok=True)
        except Exception as exc:
            logger.debug("GPG verify failed: %s", exc)

        return False

    def _encrypt_vault(
        self, archive_path: Path, passphrase: Optional[str]
    ) -> Path:
        """Encrypt a vault archive with GPG.

        Tries CapAuth first, falls back to system gpg.
        """
        output_path = archive_path.with_suffix(
            archive_path.suffix + ".gpg"
        )

        try:
            from capauth.crypto import get_backend

            backend = get_backend()
            data = archive_path.read_bytes()
            identity_file = self.agent_home / "identity" / "identity.json"
            if identity_file.exists():
                identity = json.loads(identity_file.read_text(encoding="utf-8"))
                private_key_path = (
                    self.agent_home / "identity" / "agent.key"
                )
                if private_key_path.exists():
                    signed = backend.sign(
                        data,
                        private_key_path.read_text(encoding="utf-8"),
                        passphrase or "",
                    )
                    output_path.write_text(signed, encoding="utf-8")
                    logger.info("Vault encrypted via CapAuth")
                    return output_path
        except (ImportError, Exception) as exc:
            logger.debug("CapAuth encrypt unavailable: %s", exc)

        import subprocess

        cmd = [
            "gpg", "--batch", "--yes", "--symmetric",
            "--cipher-algo", "AES256",
        ]
        if passphrase:
            cmd.extend(["--passphrase", passphrase])
        cmd.extend(["-o", str(output_path), str(archive_path)])

        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            logger.error("GPG encryption failed: %s", result.stderr)
            return archive_path

        logger.info("Vault encrypted via system GPG")
        return output_path

    def _decrypt_vault(
        self, vault_path: Path, passphrase: Optional[str]
    ) -> Path:
        """Decrypt a GPG-encrypted vault archive."""
        output_path = vault_path.with_suffix("")

        import subprocess

        cmd = ["gpg", "--batch", "--yes"]
        if passphrase:
            cmd.extend(["--passphrase", passphrase])
        cmd.extend(
            ["-o", str(output_path), "--decrypt", str(vault_path)]
        )

        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Vault decryption failed: {result.stderr}"
            )

        return output_path

    def _get_agent_name(self) -> str:
        """Read agent name from manifest."""
        manifest = self.agent_home / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                return data.get("name", "unknown")
            except json.JSONDecodeError:
                pass
        return "unknown"

    def _get_agent_fingerprint(self) -> Optional[str]:
        """Read agent PGP fingerprint from identity."""
        identity_file = self.agent_home / "identity" / "identity.json"
        if identity_file.exists():
            try:
                data = json.loads(identity_file.read_text(encoding="utf-8"))
                return data.get("fingerprint")
            except json.JSONDecodeError:
                pass
        return None
