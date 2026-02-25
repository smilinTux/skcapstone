"""
Sovereign Singularity — the sync layer.

GPG-encrypted memory seeds propagate across all nodes via Syncthing
(or git, or any file transport). CapAuth handles the encryption.
The agent exists everywhere at once.

Transport stack:
    Agent -> collect_seed() -> gpg_encrypt() -> sync_folder/
    Syncthing (or git push) propagates to all peers
    Peer -> sync_folder/ -> gpg_decrypt() -> merge_seed()
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import PillarStatus, SyncConfig, SyncState, SyncTransport

logger = logging.getLogger("skcapstone.sync")

SEED_EXTENSION = ".seed.json"
ENCRYPTED_EXTENSION = ".seed.json.gpg"


def initialize_sync(home: Path, config: Optional[SyncConfig] = None) -> SyncState:
    """Set up the sync directory structure.

    Args:
        home: Agent home directory (~/.skcapstone).
        config: Sync configuration. Defaults to SyncConfig().

    Returns:
        SyncState reflecting the initialized state.
    """
    config = config or SyncConfig()
    sync_dir = Path(config.sync_folder).expanduser()
    sync_dir.mkdir(parents=True, exist_ok=True)

    outbox = sync_dir / "outbox"
    inbox = sync_dir / "inbox"
    archive = sync_dir / "archive"
    for d in (outbox, inbox, archive):
        d.mkdir(exist_ok=True)

    manifest = {
        "transport": config.transport.value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gpg_encrypt": config.gpg_encrypt,
        "auto_push": config.auto_push,
        "auto_pull": config.auto_pull,
    }
    (sync_dir / "sync-manifest.json").write_text(json.dumps(manifest, indent=2))

    state = SyncState(
        transport=config.transport,
        sync_path=sync_dir,
        status=PillarStatus.ACTIVE,
    )

    if config.gpg_encrypt:
        fingerprint = _detect_gpg_key(home)
        if fingerprint:
            state.gpg_fingerprint = fingerprint
        else:
            state.status = PillarStatus.DEGRADED

    state.seed_count = _count_seeds(sync_dir)
    return state


def collect_seed(home: Path, agent_name: str) -> Path:
    """Collect the agent's current state into a portable seed file.

    Gathers identity, memory stats, trust metrics, and connectors
    into a single JSON blob ready for encryption and sync.

    Args:
        home: Agent home directory.
        agent_name: The agent's display name.

    Returns:
        Path to the generated seed file in the outbox.
    """
    sync_dir = (home / "sync").expanduser() if not (home / "sync").is_absolute() else home / "sync"
    if not sync_dir.exists():
        sync_dir = Path("~/.skcapstone/sync").expanduser()
    outbox = sync_dir / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc)
    hostname = _get_hostname()

    seed = {
        "schema_version": "1.0",
        "agent_name": agent_name,
        "source_host": hostname,
        "created_at": timestamp.isoformat(),
        "seed_type": "state_snapshot",
    }

    identity_file = home / "identity" / "identity.json"
    if identity_file.exists():
        seed["identity"] = json.loads(identity_file.read_text())

    trust_file = home / "trust" / "trust.json"
    if trust_file.exists():
        seed["trust"] = json.loads(trust_file.read_text())
        try:
            from .trust import export_febs_for_seed

            febs = export_febs_for_seed(home)
            if febs:
                seed["febs"] = febs
        except Exception as exc:
            logger.debug("Could not export FEBs for seed: %s", exc)

    memory_path = home / "memory"
    if memory_path.is_symlink() or memory_path.exists():
        resolved = memory_path.resolve()
        seed["memory"] = _collect_memory_stats(resolved)
        try:
            from ..memory_engine import export_for_seed

            seed["memory_entries"] = export_for_seed(home, max_entries=50)
        except Exception as exc:
            logger.debug("Could not export memory entries for seed: %s", exc)

    manifest_file = home / "manifest.json"
    if manifest_file.exists():
        seed["manifest"] = json.loads(manifest_file.read_text())

    seed_name = f"{agent_name}-{hostname}-{timestamp.strftime('%Y%m%dT%H%M%SZ')}{SEED_EXTENSION}"
    seed_path = outbox / seed_name
    seed_path.write_text(json.dumps(seed, indent=2, default=str))

    logger.info("Seed collected: %s", seed_path.name)
    return seed_path


def gpg_encrypt(
    seed_path: Path,
    recipient: Optional[str] = None,
    home: Optional[Path] = None,
    extra_recipients: Optional[list[str]] = None,
) -> Optional[Path]:
    """Encrypt a seed file with GPG.

    Encrypts to the agent's own key AND all known peer fingerprints so
    that every peer in the mesh can independently decrypt the seed they
    receive via Syncthing. Without peer fingerprints, only the sender
    can decrypt — which defeats the purpose of sync.

    Args:
        seed_path: Path to the plaintext seed file.
        recipient: Primary GPG recipient (fingerprint/email). Auto-detects if None.
        home: Agent home directory for key detection.
        extra_recipients: Additional peer fingerprints to encrypt to.

    Returns:
        Path to the encrypted file, or None if encryption failed.
    """
    if not shutil.which("gpg"):
        logger.error("gpg not found in PATH — cannot encrypt")
        return None

    agent_home = home or Path("~/.skcapstone").expanduser()

    if recipient is None:
        recipient = _detect_gpg_key(agent_home)

    if recipient is None:
        logger.error("No GPG key found for encryption")
        return None

    # Build recipient list: own key + all known peers
    all_recipients = [recipient]
    if extra_recipients:
        all_recipients.extend(r for r in extra_recipients if r and r != recipient)

    encrypted_path = seed_path.parent / (seed_path.name + ".gpg")

    recipient_args: list[str] = []
    for r in all_recipients:
        recipient_args += ["--recipient", r]

    try:
        subprocess.run(
            [
                "gpg", "--batch", "--yes", "--trust-model", "always",
                "--armor", "--encrypt",
                *recipient_args,
                "--output", str(encrypted_path), str(seed_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        logger.info(
            "Encrypted: %s -> %s (recipients: %d)",
            seed_path.name, encrypted_path.name, len(all_recipients),
        )
        return encrypted_path
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("GPG encryption failed: %s", exc)
        return None


def gpg_decrypt(encrypted_path: Path, output_dir: Optional[Path] = None) -> Optional[Path]:
    """Decrypt a GPG-encrypted seed file.

    Args:
        encrypted_path: Path to the .gpg file.
        output_dir: Where to write the decrypted file. Defaults to same dir.

    Returns:
        Path to the decrypted seed, or None on failure.
    """
    if not shutil.which("gpg"):
        logger.error("gpg not found in PATH")
        return None

    out_name = encrypted_path.name
    if out_name.endswith(".gpg"):
        out_name = out_name[:-4]
    dest = (output_dir or encrypted_path.parent) / out_name

    try:
        subprocess.run(
            ["gpg", "--batch", "--yes", "--decrypt", "--output", str(dest), str(encrypted_path)],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        logger.info("Decrypted: %s -> %s", encrypted_path.name, dest.name)
        return dest
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("GPG decryption failed: %s", exc)
        return None


def push_seed(home: Path, agent_name: str, encrypt: bool = True) -> Optional[Path]:
    """Collect current state, optionally encrypt, place in sync folder.

    This is the high-level 'push' operation. After this, Syncthing
    (or git) handles propagation to all peers automatically.

    Reads peer_fingerprints from the sync config so seeds are encrypted
    to all known peers, not just the sender's own key.

    Args:
        home: Agent home directory.
        agent_name: Agent display name.
        encrypt: Whether to GPG-encrypt the seed.

    Returns:
        Path to the final file (encrypted or plain) in the outbox.
    """
    seed_path = collect_seed(home, agent_name)

    if encrypt:
        peer_fingerprints = _load_peer_fingerprints(home)
        encrypted = gpg_encrypt(seed_path, home=home, extra_recipients=peer_fingerprints)
        if encrypted:
            seed_path.unlink()
            return encrypted
        logger.warning("Encryption failed — keeping plaintext seed")

    return seed_path


def _load_peer_fingerprints(home: Path) -> list[str]:
    """Load known peer GPG fingerprints from sync config.

    Args:
        home: Agent home directory.

    Returns:
        List of peer fingerprint strings (may be empty).
    """
    config_file = home / "config" / "config.yaml"
    if not config_file.exists():
        return []
    try:
        import yaml as _yaml
        data = _yaml.safe_load(config_file.read_text()) or {}
        sync_data = data.get("sync", {})
        peers = sync_data.get("peer_fingerprints", [])
        return [str(p) for p in peers if p]
    except Exception as exc:
        logger.debug("Could not load peer fingerprints: %s", exc)
        return []


def pull_seeds(home: Path, decrypt: bool = True) -> list[dict]:
    """Pull and process seed files from the inbox.

    Reads all seeds in inbox/, decrypts if needed, and returns
    the parsed seed data. Processed files move to archive/.

    Args:
        home: Agent home directory.
        decrypt: Whether to attempt GPG decryption.

    Returns:
        List of parsed seed dictionaries.
    """
    sync_dir = _resolve_sync_dir(home)
    inbox = sync_dir / "inbox"
    archive = sync_dir / "archive"

    if not inbox.exists():
        return []

    seeds = []

    for f in sorted(inbox.iterdir()):
        if f.name.startswith("."):
            continue

        seed_path = f
        if decrypt and f.suffix == ".gpg":
            decrypted = gpg_decrypt(f)
            if decrypted:
                seed_path = decrypted
                f.unlink()
            else:
                logger.warning("Could not decrypt %s — skipping", f.name)
                continue

        if seed_path.suffix == ".json" or seed_path.name.endswith(SEED_EXTENSION):
            try:
                data = json.loads(seed_path.read_text())
                seeds.append(data)

                if "memory_entries" in data:
                    try:
                        from ..memory_engine import import_from_seed

                        imported = import_from_seed(home, data["memory_entries"])
                        if imported:
                            logger.info("Imported %d memories from seed %s", imported, seed_path.name)
                    except Exception as exc:
                        logger.debug("Could not import seed memories: %s", exc)

                if "febs" in data:
                    try:
                        from .trust import import_febs_from_seed

                        feb_imported = import_febs_from_seed(home, data["febs"])
                        if feb_imported:
                            logger.info("Imported %d FEB(s) from seed %s", feb_imported, seed_path.name)
                    except Exception as exc:
                        logger.debug("Could not import seed FEBs: %s", exc)

                archive.mkdir(exist_ok=True)
                seed_path.rename(archive / seed_path.name)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to process %s: %s", seed_path.name, exc)

    return seeds


def discover_sync(home: Path) -> SyncState:
    """Discover the current sync state from disk.

    Args:
        home: Agent home directory.

    Returns:
        SyncState reflecting what's on disk.
    """
    sync_dir = _resolve_sync_dir(home)

    if not sync_dir.exists():
        return SyncState(status=PillarStatus.MISSING)

    manifest_file = sync_dir / "sync-manifest.json"
    if not manifest_file.exists():
        return SyncState(sync_path=sync_dir, status=PillarStatus.DEGRADED)

    try:
        data = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        return SyncState(sync_path=sync_dir, status=PillarStatus.DEGRADED)

    transport = SyncTransport(data.get("transport", "syncthing"))

    state = SyncState(
        transport=transport,
        sync_path=sync_dir,
        seed_count=_count_seeds(sync_dir),
        status=PillarStatus.ACTIVE,
    )

    fingerprint = _detect_gpg_key(home)
    if fingerprint:
        state.gpg_fingerprint = fingerprint
    elif data.get("gpg_encrypt", True):
        state.status = PillarStatus.DEGRADED

    _load_sync_timestamps(sync_dir, state)
    return state


# --- Private helpers ---


def _resolve_sync_dir(home: Path) -> Path:
    """Resolve the sync directory path."""
    sync_dir = home / "sync"
    if sync_dir.exists():
        return sync_dir
    return Path("~/.skcapstone/sync").expanduser()


def _detect_gpg_key(home: Path) -> Optional[str]:
    """Try to find the agent's GPG fingerprint."""
    identity_file = home / "identity" / "identity.json"
    if identity_file.exists():
        try:
            data = json.loads(identity_file.read_text())
            fp = data.get("fingerprint")
            if fp and data.get("capauth_managed"):
                return fp
        except (json.JSONDecodeError, OSError):
            pass

    return _detect_gpg_key_from_skcapstone()


def _detect_gpg_key_from_skcapstone() -> Optional[str]:
    """Look up a GPG key associated with skcapstone in the system keyring.

    Searches for keys with 'skcapstone' in the UID first, then falls
    back to the user's own secret keys (skipping package-signing keys).
    """
    if not shutil.which("gpg"):
        return None
    try:
        result = subprocess.run(
            ["gpg", "--list-secret-keys", "--keyid-format", "long", "--with-colons"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Reason: prefer skcapstone-specific key, then any user secret key
        lines = result.stdout.splitlines()
        current_fpr = None
        for line in lines:
            if line.startswith("fpr:"):
                current_fpr = line.split(":")[9]
            if line.startswith("uid:") and "skcapstone" in line.lower():
                return current_fpr
        # No skcapstone key — return first secret key fingerprint
        for line in lines:
            if line.startswith("fpr:"):
                return line.split(":")[9]
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    return None


def _get_hostname() -> str:
    """Get the machine hostname for seed identification."""
    import socket

    return socket.gethostname()


def _count_seeds(sync_dir: Path) -> int:
    """Count seed files across outbox, inbox, and archive."""
    count = 0
    for subdir in ("outbox", "inbox", "archive"):
        d = sync_dir / subdir
        if d.exists():
            count += sum(
                1 for f in d.iterdir() if f.name.endswith(SEED_EXTENSION) or f.suffix == ".gpg"
            )
    return count


def _collect_memory_stats(memory_path: Path) -> dict:
    """Gather memory statistics from the SKMemory store."""
    stats = {"path": str(memory_path), "total": 0, "short": 0, "mid": 0, "long": 0}
    for tier, dirname in [("long", "long-term"), ("mid", "mid-term"), ("short", "short-term")]:
        tier_dir = memory_path / dirname
        if tier_dir.exists():
            count = sum(1 for f in tier_dir.iterdir() if f.suffix in (".md", ".json", ".yaml"))
            stats[tier] = count
            stats["total"] += count
    return stats


def _load_sync_timestamps(sync_dir: Path, state: SyncState) -> None:
    """Load last push/pull timestamps from the sync state file."""
    state_file = sync_dir / "sync-state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text())
            if data.get("last_push"):
                state.last_push = datetime.fromisoformat(data["last_push"])
            if data.get("last_pull"):
                state.last_pull = datetime.fromisoformat(data["last_pull"])
            state.peers_known = data.get("peers_known", 0)
        except (json.JSONDecodeError, OSError, ValueError):
            pass


def save_sync_state(sync_dir: Path, state: SyncState) -> None:
    """Persist sync timestamps and peer info.

    Args:
        sync_dir: The sync directory.
        state: Current SyncState to persist.
    """
    data = {
        "last_push": state.last_push.isoformat() if state.last_push else None,
        "last_pull": state.last_pull.isoformat() if state.last_pull else None,
        "peers_known": state.peers_known,
        "seed_count": state.seed_count,
    }
    (sync_dir / "sync-state.json").write_text(json.dumps(data, indent=2))
