"""
The Vault -- encrypted agent state bundling.

Never sync raw state. Package it. Encrypt it. Sign it.
Then let the transport layer do its job.

The vault is a tarball of selected ~/.skcapstone/ directories,
encrypted with PGP (via CapAuth) and signed to prove authenticity.
"""

from __future__ import annotations

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
    ) -> Path:
        """Pack agent state into a vault archive.

        Args:
            pillars: Which pillars to include. Defaults to all syncable pillars.
            encrypt: Whether to GPG-encrypt the archive.
            passphrase: Passphrase for encryption (required if encrypt=True).

        Returns:
            Path to the created vault file (.tar.gz or .tar.gz.gpg).
        """
        target_pillars = pillars or PILLARS_TO_SYNC
        hostname = os.uname().nodename
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive_name = f"vault-{hostname}-{timestamp}.tar.gz"
        archive_path = self.vault_dir / archive_name

        included = []
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

                included.append(pillar)

            manifest_path = self.agent_home / "manifest.json"
            if manifest_path.exists():
                tar.add(
                    str(manifest_path),
                    arcname="manifest.json",
                )

        manifest = VaultManifest(
            agent_name=self._get_agent_name(),
            source_host=hostname,
            created_at=datetime.now(timezone.utc),
            pillars_included=included,
            encrypted=encrypt,
        )

        manifest_file = archive_path.with_suffix(".manifest.json")
        manifest_file.write_text(
            manifest.model_dump_json(indent=2)
        )

        if encrypt:
            encrypted_path = self._encrypt_vault(
                archive_path, passphrase
            )
            archive_path.unlink()
            logger.info("Vault packed and encrypted: %s", encrypted_path)
            return encrypted_path

        logger.info(
            "Vault packed: %s (%d pillars)",
            archive_path,
            len(included),
        )
        return archive_path

    def unpack(
        self,
        vault_path: Path,
        decrypt: bool = False,
        passphrase: Optional[str] = None,
        target: Optional[Path] = None,
    ) -> Path:
        """Unpack a vault archive to restore agent state.

        Args:
            vault_path: Path to the vault file.
            decrypt: Whether the vault is GPG-encrypted.
            passphrase: Passphrase for decryption.
            target: Where to extract. Defaults to agent_home.

        Returns:
            Path to the extraction directory.
        """
        extract_to = target or self.agent_home

        if decrypt:
            vault_path = self._decrypt_vault(vault_path, passphrase)

        manifest_file = vault_path.with_suffix(".manifest.json")
        if manifest_file.exists():
            manifest_data = json.loads(manifest_file.read_text())
            manifest = VaultManifest(**manifest_data)
            logger.info(
                "Restoring vault from %s (agent=%s, pillars=%s)",
                manifest.source_host,
                manifest.agent_name,
                manifest.pillars_included,
            )

        with tarfile.open(vault_path, "r:gz") as tar:
            tar.extractall(path=extract_to, filter="data")

        logger.info("Vault unpacked to %s", extract_to)
        return extract_to

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
            meta = {"path": f, "size": f.stat().st_size}
            if manifest_file.exists():
                try:
                    data = json.loads(manifest_file.read_text())
                    meta.update(data)
                except json.JSONDecodeError:
                    pass
            vaults.append(meta)
        return vaults

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
                identity = json.loads(identity_file.read_text())
                private_key_path = (
                    self.agent_home / "identity" / "agent.key"
                )
                if private_key_path.exists():
                    signed = backend.sign(
                        data,
                        private_key_path.read_text(),
                        passphrase or "",
                    )
                    output_path.write_text(signed)
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
                data = json.loads(manifest.read_text())
                return data.get("name", "unknown")
            except json.JSONDecodeError:
                pass
        return "unknown"
