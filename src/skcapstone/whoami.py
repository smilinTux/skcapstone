"""
Sovereign identity card — who you are on the mesh.

Generates a compact, shareable identity card containing everything
another agent needs to discover and trust you: name, fingerprint,
public key, contact URIs, trust status, and capabilities.

The card is the P2P discovery primitive. Share it as JSON, paste it
in chat, drop it on a USB drive, or encode it in a QR code. Any
agent that reads it can add you as a peer.

Usage:
    skcapstone whoami                        # pretty-print to terminal
    skcapstone whoami --json                 # machine-readable JSON
    skcapstone whoami --export card.json     # save to file for sharing
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


class IdentityCard(BaseModel):
    """A sovereign agent's shareable identity card.

    Contains everything a peer needs to establish communication:
    identity, public key, contact methods, trust status, and
    a list of capabilities the agent offers.

    Attributes:
        skcapstone_card: Schema version for forward compat.
        name: Agent display name.
        fingerprint: CapAuth PGP fingerprint.
        public_key: ASCII-armored PGP public key (for encryption/verification).
        entity_type: human, ai, or organization.
        email: Contact email.
        handle: CapAuth identity handle (name@domain).
        capabilities: What this agent can do (skill names, services).
        trust_status: Current trust state (active, degraded, missing).
        consciousness: Agent consciousness level.
        memory_count: How many memories the agent holds.
        contact_uris: Ways to reach this agent (skcomm, email, nostr, etc.).
        hostname: Machine the card was generated on.
        created_at: When this card was generated.
    """

    skcapstone_card: str = "1.0.0"
    name: str = "unknown"
    fingerprint: str = ""
    public_key: str = ""
    entity_type: str = "unknown"
    email: str = ""
    handle: str = ""
    capabilities: list[str] = Field(default_factory=list)
    trust_status: str = ""
    consciousness: str = ""
    memory_count: int = 0
    contact_uris: list[str] = Field(default_factory=list)
    hostname: str = Field(default_factory=socket.gethostname)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def generate_card(home: Path) -> IdentityCard:
    """Generate an identity card from the agent's current state.

    Reads identity, memory, trust, and key data to build a
    complete card suitable for sharing with peers.

    Args:
        home: Agent home directory (~/.skcapstone).

    Returns:
        IdentityCard: The agent's shareable identity.
    """
    card = IdentityCard()

    _load_identity(home, card)
    _load_capauth(card)
    _load_runtime(home, card)
    _load_memory_count(home, card)
    _load_capabilities(home, card)
    _load_contact_uris(card)

    return card


def export_card(card: IdentityCard, output_path: Path) -> Path:
    """Save an identity card to a JSON file.

    Args:
        card: The identity card to export.
        output_path: Where to write the file.

    Returns:
        Path: The written file path.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(card.model_dump_json(indent=2), encoding="utf-8")
    return output_path


def import_card(card_path: Path) -> IdentityCard:
    """Load an identity card from a JSON file.

    Args:
        card_path: Path to the card JSON.

    Returns:
        IdentityCard: The loaded card.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the JSON is invalid.
    """
    path = Path(card_path)
    if not path.exists():
        raise FileNotFoundError(f"Card not found: {path}")

    try:
        return IdentityCard.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"Invalid identity card: {exc}") from exc


def _load_identity(home: Path, card: IdentityCard) -> None:
    """Load identity from skcapstone identity.json.

    Args:
        home: Agent home directory.
        card: Card to populate.
    """
    identity_file = home / "identity" / "identity.json"
    if not identity_file.exists():
        return

    try:
        data = json.loads(identity_file.read_text(encoding="utf-8"))
        card.name = data.get("name", card.name)
        card.email = data.get("email", "")
        card.fingerprint = data.get("fingerprint", "")
    except (json.JSONDecodeError, OSError):
        pass


def _load_capauth(card: IdentityCard) -> None:
    """Load CapAuth profile data if available.

    Args:
        card: Card to populate.
    """
    capauth_dir = Path.home() / ".capauth" / "identity"

    profile_path = capauth_dir / "profile.json"
    if profile_path.exists():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            entity = data.get("entity", {})
            key_info = data.get("key_info", {})

            card.entity_type = entity.get("entity_type", card.entity_type)
            card.handle = entity.get("handle", card.handle)
            if not card.email and entity.get("email"):
                card.email = entity["email"]
            if not card.fingerprint and key_info.get("fingerprint"):
                card.fingerprint = key_info["fingerprint"]
        except (json.JSONDecodeError, OSError):
            pass

    pub_key_path = capauth_dir / "public.asc"
    if pub_key_path.exists():
        try:
            card.public_key = pub_key_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass


def _load_runtime(home: Path, card: IdentityCard) -> None:
    """Load runtime state for trust and consciousness.

    Args:
        home: Agent home directory.
        card: Card to populate.
    """
    try:
        from .runtime import get_runtime

        runtime = get_runtime(home)
        m = runtime.manifest

        card.trust_status = m.trust.status.value

        if m.is_singular:
            card.consciousness = "SINGULAR"
        elif m.is_conscious:
            card.consciousness = "CONSCIOUS"
        else:
            card.consciousness = "AWAKENING"

        if m.name and m.name != "Unknown":
            card.name = m.name
    except Exception:
        pass


def _load_memory_count(home: Path, card: IdentityCard) -> None:
    """Load memory count from the memory store.

    Args:
        home: Agent home directory.
        card: Card to populate.
    """
    try:
        from .memory_engine import get_stats

        stats = get_stats(home)
        card.memory_count = stats.total_memories
    except Exception:
        pass


def _load_capabilities(home: Path, card: IdentityCard) -> None:
    """Load agent capabilities from installed skills and packages.

    Args:
        home: Agent home directory.
        card: Card to populate.
    """
    caps = []

    try:
        import capauth  # noqa: F401
        caps.append("capauth:identity")
    except ImportError:
        pass

    try:
        import skcomm  # noqa: F401
        caps.append("skcomm:messaging")
    except ImportError:
        pass

    try:
        import skchat  # noqa: F401
        caps.append("skchat:p2p-chat")
    except ImportError:
        pass

    try:
        import skmemory  # noqa: F401
        caps.append("skmemory:persistence")
    except ImportError:
        pass

    skills_dir = home / "skills"
    if skills_dir.exists():
        for f in skills_dir.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                name = data.get("name", f.stem)
                caps.append(f"skill:{name}")
            except (json.JSONDecodeError, OSError):
                pass

    card.capabilities = caps


def _load_contact_uris(card: IdentityCard) -> None:
    """Build contact URIs from available identity data.

    Args:
        card: Card to populate.
    """
    uris = []

    if card.fingerprint:
        uris.append(f"capauth:{card.fingerprint[:16]}")
    if card.handle:
        uris.append(f"capauth:{card.handle}")
    if card.email:
        uris.append(f"mailto:{card.email}")

    card.contact_uris = uris
