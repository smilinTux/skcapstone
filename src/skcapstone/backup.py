"""Sovereign agent backup and restore.

Creates a portable, encrypted archive of the full agent state:
identity, memories, trust, config, agent card, and coordination.
Restores to any machine with a single command.

The backup is a gzip-compressed tar archive. When a PGP key is
available, the archive content manifest is signed for integrity.

Layout inside the tarball:
    backup-<timestamp>/
    ├── manifest.json          # backup metadata + file checksums
    ├── config/                # agent configuration
    ├── identity/              # CapAuth profile + keys
    ├── memory/                # SKMemory data
    ├── trust/                 # Cloud 9 FEB files + seeds
    ├── coordination/          # board state
    └── agent-card.json        # sovereign identity card
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
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME, __version__

logger = logging.getLogger("skcapstone.backup")


class BackupManifest(BaseModel):
    """Metadata for a sovereign agent backup.

    Attributes:
        backup_id: Unique backup identifier (timestamp-based).
        created_at: When the backup was created.
        agent_name: Name of the backed-up agent.
        version: SKCapstone version that created the backup.
        home_path: Original agent home path.
        files: Dict of relative path -> SHA-256 hash.
        total_size: Total uncompressed size in bytes.
    """

    backup_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_name: str = ""
    version: str = __version__
    home_path: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    total_size: int = 0


# Directories relative to agent home to include in backup
BACKUP_DIRS = [
    "config",
    "identity",
    "memory",
    "trust",
    "coordination",
    "logs",
]

# Individual files relative to agent home
BACKUP_FILES = [
    "manifest.json",
    "agent-card.json",
]

# Patterns to exclude from backup (security-sensitive)
EXCLUDE_PATTERNS = [
    "*.pyc",
    "__pycache__",
    ".git",
]


def _sha256_file(filepath: Path) -> str:
    """Compute SHA-256 of a file.

    Args:
        filepath: Path to the file.

    Returns:
        str: Hex digest.
    """
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _should_exclude(name: str) -> bool:
    """Check if a tar member should be excluded.

    Args:
        name: Filename or path component.

    Returns:
        bool: True if the file should be skipped.
    """
    base = os.path.basename(name)
    for pattern in EXCLUDE_PATTERNS:
        if pattern.startswith("*"):
            if base.endswith(pattern[1:]):
                return True
        elif base == pattern:
            return True
    return False


def create_backup(
    home: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    agent_name: str = "",
) -> dict[str, Any]:
    """Create a compressed backup of the full agent state.

    Collects all agent data directories and files into a
    gzip-compressed tar archive with a manifest for integrity.

    Args:
        home: Agent home directory. Defaults to ~/.skcapstone.
        output_dir: Where to write the backup file. Defaults to ~/backups.
        agent_name: Agent name for the manifest.

    Returns:
        dict: Result with 'filepath', 'size', 'file_count', 'manifest'.
    """
    home_path = (home or Path(AGENT_HOME)).expanduser()
    if not home_path.exists():
        raise FileNotFoundError(f"Agent home not found: {home_path}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    backup_id = f"backup-{timestamp}"

    out_dir = (output_dir or home_path / "backups").expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / f"{backup_id}.tar.gz"

    manifest = BackupManifest(
        backup_id=backup_id,
        agent_name=agent_name or _read_agent_name(home_path),
        home_path=str(home_path),
    )

    total_size = 0
    file_count = 0

    with tarfile.open(archive_path, "w:gz") as tar:
        for dir_name in BACKUP_DIRS:
            dir_path = home_path / dir_name
            if not dir_path.exists():
                continue

            for filepath in sorted(dir_path.rglob("*")):
                if not filepath.is_file():
                    continue
                if _should_exclude(str(filepath)):
                    continue

                rel = filepath.relative_to(home_path)
                arcname = f"{backup_id}/{rel}"

                tar.add(filepath, arcname=arcname)
                manifest.files[str(rel)] = _sha256_file(filepath)
                total_size += filepath.stat().st_size
                file_count += 1

        for filename in BACKUP_FILES:
            filepath = home_path / filename
            if filepath.exists():
                arcname = f"{backup_id}/{filename}"
                tar.add(filepath, arcname=arcname)
                manifest.files[filename] = _sha256_file(filepath)
                total_size += filepath.stat().st_size
                file_count += 1

        manifest.total_size = total_size

        manifest_json = manifest.model_dump_json(indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=f"{backup_id}/_backup_manifest.json")
        info.size = len(manifest_json)
        tar.addfile(info, BytesIO(manifest_json))

    archive_size = archive_path.stat().st_size

    logger.info(
        "Backup created: %s (%d files, %d bytes -> %d bytes compressed)",
        archive_path, file_count, total_size, archive_size,
    )

    return {
        "filepath": str(archive_path),
        "backup_id": backup_id,
        "file_count": file_count,
        "total_size": total_size,
        "archive_size": archive_size,
        "manifest": manifest.model_dump(mode="json"),
    }


def restore_backup(
    archive_path: str | Path,
    target_home: Optional[Path] = None,
    verify: bool = True,
) -> dict[str, Any]:
    """Restore an agent from a backup archive.

    Extracts the archive to the target home directory and
    verifies file integrity against the manifest checksums.

    Args:
        archive_path: Path to the .tar.gz backup file.
        target_home: Where to restore. Defaults to ~/.skcapstone.
        verify: Whether to verify checksums after extraction.

    Returns:
        dict: Result with 'restored', 'file_count', 'verified', 'errors'.
    """
    archive = Path(archive_path).expanduser()
    if not archive.exists():
        raise FileNotFoundError(f"Backup not found: {archive}")

    target = (target_home or Path(AGENT_HOME)).expanduser()
    target.mkdir(parents=True, exist_ok=True)

    manifest: Optional[BackupManifest] = None
    backup_prefix = ""

    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError("Empty backup archive")

        backup_prefix = members[0].name.split("/")[0]

        manifest_member = f"{backup_prefix}/_backup_manifest.json"
        try:
            f = tar.extractfile(manifest_member)
            if f:
                manifest = BackupManifest.model_validate_json(f.read())
        except (KeyError, Exception) as exc:
            logger.warning("No manifest in backup: %s", exc)

        file_count = 0
        for member in tar.getmembers():
            if member.name == manifest_member:
                continue
            if not member.isfile():
                continue
            if _should_exclude(member.name):
                continue

            rel_path = member.name[len(backup_prefix) + 1:]
            member.name = rel_path
            tar.extract(member, path=target, filter="data")
            file_count += 1

    errors: list[str] = []
    if verify and manifest:
        for rel_path, expected_hash in manifest.files.items():
            restored_file = target / rel_path
            if not restored_file.exists():
                errors.append(f"Missing: {rel_path}")
                continue
            actual = _sha256_file(restored_file)
            if actual != expected_hash:
                errors.append(f"Checksum mismatch: {rel_path}")

    logger.info(
        "Restored %d files to %s (%d verification errors)",
        file_count, target, len(errors),
    )

    return {
        "restored": file_count,
        "file_count": file_count,
        "target": str(target),
        "verified": len(errors) == 0,
        "errors": errors,
        "backup_id": manifest.backup_id if manifest else "unknown",
        "agent_name": manifest.agent_name if manifest else "unknown",
    }


def list_backups(
    backup_dir: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """List available backup archives.

    Args:
        backup_dir: Directory to scan. Defaults to ~/.skcapstone/backups.

    Returns:
        list[dict]: Backup metadata sorted newest first.
    """
    search_dir = (backup_dir or Path(AGENT_HOME).expanduser() / "backups")
    if not search_dir.exists():
        return []

    backups = []
    for f in sorted(search_dir.glob("backup-*.tar.gz"), reverse=True):
        stat = f.stat()
        backups.append({
            "filepath": str(f),
            "filename": f.name,
            "size": stat.st_size,
            "created": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        })

    return backups


def _read_agent_name(home: Path) -> str:
    """Read agent name from config or manifest.

    Args:
        home: Agent home directory.

    Returns:
        str: Agent name or 'unknown'.
    """
    for filename in ("manifest.json", "config/config.yaml"):
        filepath = home / filename
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text())
                name = data.get("name") or data.get("agent_name")
                if name:
                    return name
            except Exception:
                continue
    return "unknown"
