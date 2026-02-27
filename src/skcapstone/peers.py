"""
Sovereign peer management — the other half of P2P discovery.

whoami exports your identity card. This module imports someone
else's card and registers them as a peer in the SKComm keystore.
The two together form the complete P2P discovery loop.

Flow:
    1. Agent A runs: skcapstone whoami --export card.json
    2. Agent A shares card.json with Agent B (USB, chat, email, QR)
    3. Agent B runs: skcapstone peer add --card card.json
    4. Agent B can now send encrypted messages to Agent A

Peer data is stored at:
    ~/.skcomm/peers/<name>.yml     — SKComm peer config
    ~/.skcapstone/peers/<name>.json — Extended peer metadata
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.peers")


class PeerRecord(BaseModel):
    """A known peer agent.

    Attributes:
        name: Peer display name.
        fingerprint: CapAuth PGP fingerprint.
        public_key: ASCII-armored PGP public key.
        entity_type: human, ai, or organization.
        handle: CapAuth identity handle.
        email: Contact email.
        capabilities: What this peer can do.
        contact_uris: How to reach this peer.
        trust_level: verified, trusted, sovereign, or unknown.
        added_at: When the peer was added.
        last_seen: Last known activity.
        source: How we learned about this peer (card, discovery, manual).
    """

    name: str
    fingerprint: str = ""
    public_key: str = ""
    entity_type: str = "unknown"
    handle: str = ""
    email: str = ""
    capabilities: list[str] = Field(default_factory=list)
    contact_uris: list[str] = Field(default_factory=list)
    trust_level: str = "unknown"
    added_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_seen: Optional[str] = None
    source: str = "manual"


def add_peer_from_card(
    card_path: Path,
    skcapstone_home: Optional[Path] = None,
    skcomm_home: Optional[Path] = None,
) -> PeerRecord:
    """Import a peer from a whoami identity card.

    Reads the card JSON, creates peer records in both skcapstone
    and skcomm directories, and writes the public key for SKComm
    encryption.

    Args:
        card_path: Path to the exported card.json.
        skcapstone_home: Override skcapstone home. Defaults to ~/.skcapstone/.
        skcomm_home: Override skcomm home. Defaults to ~/.skcomm/.

    Returns:
        PeerRecord: The registered peer.

    Raises:
        FileNotFoundError: If the card file doesn't exist.
        ValueError: If the card is missing required fields.
    """
    card_path = Path(card_path)
    if not card_path.exists():
        raise FileNotFoundError(f"Card not found: {card_path}")

    try:
        card_data = json.loads(card_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid card JSON: {exc}") from exc

    name = card_data.get("name", "").strip()
    if not name:
        raise ValueError("Card is missing a 'name' field")

    peer = PeerRecord(
        name=name,
        fingerprint=card_data.get("fingerprint", ""),
        public_key=card_data.get("public_key", ""),
        entity_type=card_data.get("entity_type", "unknown"),
        handle=card_data.get("handle", ""),
        email=card_data.get("email", ""),
        capabilities=card_data.get("capabilities", []),
        contact_uris=card_data.get("contact_uris", []),
        trust_level="verified",
        source="card",
    )

    sk_home = skcapstone_home or Path.home() / ".skcapstone"
    sc_home = skcomm_home or Path.home() / ".skcomm"

    _save_skcapstone_peer(sk_home, peer)
    _save_skcomm_peer(sc_home, peer)

    logger.info("Added peer '%s' (fingerprint: %s)", name, peer.fingerprint[:16])
    return peer


def add_peer_manual(
    name: str,
    fingerprint: str = "",
    public_key_path: Optional[Path] = None,
    email: str = "",
    skcapstone_home: Optional[Path] = None,
    skcomm_home: Optional[Path] = None,
) -> PeerRecord:
    """Add a peer manually by name and optional key file.

    Args:
        name: Peer display name.
        fingerprint: PGP fingerprint (optional).
        public_key_path: Path to a .asc public key file (optional).
        email: Contact email (optional).
        skcapstone_home: Override skcapstone home.
        skcomm_home: Override skcomm home.

    Returns:
        PeerRecord: The registered peer.
    """
    public_key = ""
    if public_key_path and Path(public_key_path).exists():
        public_key = Path(public_key_path).read_text(encoding="utf-8").strip()

    peer = PeerRecord(
        name=name,
        fingerprint=fingerprint,
        public_key=public_key,
        email=email,
        source="manual",
        trust_level="verified" if public_key else "unknown",
    )

    sk_home = skcapstone_home or Path.home() / ".skcapstone"
    sc_home = skcomm_home or Path.home() / ".skcomm"

    _save_skcapstone_peer(sk_home, peer)
    _save_skcomm_peer(sc_home, peer)

    return peer


def list_peers(
    skcapstone_home: Optional[Path] = None,
) -> list[PeerRecord]:
    """List all known peers.

    Args:
        skcapstone_home: Override skcapstone home.

    Returns:
        list[PeerRecord]: All registered peers.
    """
    sk_home = skcapstone_home or Path.home() / ".skcapstone"
    peers_dir = sk_home / "peers"
    if not peers_dir.exists():
        return []

    peers = []
    for f in sorted(peers_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            peers.append(PeerRecord.model_validate(data))
        except (json.JSONDecodeError, Exception):
            continue

    return peers


def get_peer(
    name: str,
    skcapstone_home: Optional[Path] = None,
) -> Optional[PeerRecord]:
    """Get a specific peer by name.

    Args:
        name: Peer name to look up.
        skcapstone_home: Override skcapstone home.

    Returns:
        PeerRecord or None if not found.
    """
    sk_home = skcapstone_home or Path.home() / ".skcapstone"
    peer_file = sk_home / "peers" / f"{_safe_filename(name)}.json"
    if not peer_file.exists():
        return None

    try:
        data = json.loads(peer_file.read_text(encoding="utf-8"))
        return PeerRecord.model_validate(data)
    except (json.JSONDecodeError, Exception):
        return None


def remove_peer(
    name: str,
    skcapstone_home: Optional[Path] = None,
    skcomm_home: Optional[Path] = None,
) -> bool:
    """Remove a peer from both skcapstone and skcomm registries.

    Args:
        name: Peer name to remove.
        skcapstone_home: Override skcapstone home.
        skcomm_home: Override skcomm home.

    Returns:
        bool: True if the peer was found and removed.
    """
    sk_home = skcapstone_home or Path.home() / ".skcapstone"
    sc_home = skcomm_home or Path.home() / ".skcomm"
    safe = _safe_filename(name)
    removed = False

    sk_file = sk_home / "peers" / f"{safe}.json"
    if sk_file.exists():
        sk_file.unlink()
        removed = True

    sc_file = sc_home / "peers" / f"{safe}.yml"
    if sc_file.exists():
        sc_file.unlink()
        removed = True

    sc_key = sc_home / "peers" / f"{safe}.pub.asc"
    if sc_key.exists():
        sc_key.unlink()

    logger.info("Removed peer '%s'", name)
    return removed


def _save_skcapstone_peer(home: Path, peer: PeerRecord) -> Path:
    """Save peer record to skcapstone peers directory.

    Args:
        home: skcapstone home directory.
        peer: Peer to save.

    Returns:
        Path: Written file path.
    """
    peers_dir = home / "peers"
    peers_dir.mkdir(parents=True, exist_ok=True)

    path = peers_dir / f"{_safe_filename(peer.name)}.json"
    path.write_text(peer.model_dump_json(indent=2), encoding="utf-8")
    return path


def _save_skcomm_peer(home: Path, peer: PeerRecord) -> Path:
    """Save peer to SKComm peers directory (YAML + public key).

    Creates the YAML config that SKComm's KeyStore reads, and
    writes the public key as a separate .asc file.

    Args:
        home: skcomm home directory.
        peer: Peer to save.

    Returns:
        Path: Written YAML file path.
    """
    peers_dir = home / "peers"
    peers_dir.mkdir(parents=True, exist_ok=True)

    safe = _safe_filename(peer.name)

    if peer.public_key:
        key_path = peers_dir / f"{safe}.pub.asc"
        key_path.write_text(peer.public_key, encoding="utf-8")
        pubkey_ref = str(key_path)
    else:
        pubkey_ref = ""

    peer_yml = {
        "name": peer.name,
        "fingerprint": peer.fingerprint,
        "public_key": pubkey_ref,
        "email": peer.email,
        "trust_level": peer.trust_level,
        "added_at": peer.added_at,
    }

    path = peers_dir / f"{safe}.yml"
    path.write_text(yaml.dump(peer_yml, default_flow_style=False), encoding="utf-8")
    return path


def _safe_filename(name: str) -> str:
    """Convert a peer name to a safe filename.

    Args:
        name: Peer display name.

    Returns:
        str: Filesystem-safe version of the name.
    """
    safe = name.lower().strip().replace(" ", "-")
    safe = "".join(c for c in safe if c.isalnum() or c in "-_.")
    return safe or "unnamed"
