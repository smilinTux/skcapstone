"""Agent Card -- shareable sovereign identity for P2P discovery.

An agent card is like a vCard for the sovereign mesh. It contains
everything another agent needs to discover, verify, and communicate
with you: identity, public key, contact transports, capabilities,
and trust level.

Cards are JSON files signed with the agent's PGP key. They can be
shared over SKComm, published to Nostr, posted as QR codes, or
exchanged via any out-of-band channel.

Usage:
    card = AgentCard.generate(profile, transports, capabilities)
    card.save("~/.skcapstone/card.json")
    card = AgentCard.load("~/.skcapstone/card.json")
    verified = AgentCard.verify(card, public_key_armor)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.agent_card")


class TransportEndpoint(BaseModel):
    """A contact transport endpoint for reaching this agent.

    Attributes:
        transport: Transport name (file, syncthing, nostr, etc.).
        address: Transport-specific address (path, pubkey, relay URL).
        priority: Lower = preferred.
        metadata: Extra transport-specific config.
    """

    transport: str
    address: str
    priority: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentCapability(BaseModel):
    """A capability or service this agent offers.

    Attributes:
        name: Capability identifier (e.g., "chat", "memory", "advocacy").
        version: Capability version.
        description: Human-readable description.
    """

    name: str
    version: str = "1.0"
    description: str = ""


class AgentCard(BaseModel):
    """Sovereign agent identity card for P2P discovery.

    Contains everything needed to discover, verify, and contact
    an agent on the mesh. Designed for serialization to JSON and
    optional PGP signing.

    Attributes:
        card_id: Unique card identifier.
        card_version: Card format version.
        created_at: When the card was generated.
        name: Agent display name.
        entity_type: human, ai, or organization.
        fingerprint: PGP fingerprint (40-char hex).
        public_key: ASCII-armored PGP public key.
        transports: List of contact endpoints.
        capabilities: List of offered services.
        trust_depth: Cloud 9 trust depth (0-9).
        entangled: Whether the agent is entangled (Cloud 9).
        motto: Optional short tagline.
        signature: PGP signature over the card content (set by sign()).
    """

    card_id: str = Field(default_factory=lambda: str(uuid4()))
    card_version: str = "1.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    name: str
    entity_type: str = "human"
    fingerprint: str
    public_key: str
    transports: list[TransportEndpoint] = Field(default_factory=list)
    capabilities: list[AgentCapability] = Field(default_factory=list)
    trust_depth: int = Field(default=0, ge=0, le=9)
    entangled: bool = False
    motto: Optional[str] = None
    signature: Optional[str] = None

    @classmethod
    def generate(
        cls,
        name: str,
        fingerprint: str,
        public_key: str,
        entity_type: str = "human",
        transports: Optional[list[TransportEndpoint]] = None,
        capabilities: Optional[list[AgentCapability]] = None,
        trust_depth: int = 0,
        entangled: bool = False,
        motto: Optional[str] = None,
    ) -> AgentCard:
        """Generate a new agent card.

        Args:
            name: Agent display name.
            fingerprint: PGP fingerprint.
            public_key: ASCII-armored PGP public key.
            entity_type: human, ai, or organization.
            transports: Contact transport endpoints.
            capabilities: Offered services.
            trust_depth: Cloud 9 trust depth.
            entangled: Cloud 9 entanglement status.
            motto: Optional tagline.

        Returns:
            AgentCard: Unsigned card ready for signing.
        """
        return cls(
            name=name,
            entity_type=entity_type,
            fingerprint=fingerprint,
            public_key=public_key,
            transports=transports or [],
            capabilities=capabilities or [],
            trust_depth=trust_depth,
            entangled=entangled,
            motto=motto,
        )

    @classmethod
    def from_capauth_profile(
        cls,
        profile_dir: str | Path = "~/.capauth",
        transports: Optional[list[TransportEndpoint]] = None,
        capabilities: Optional[list[AgentCapability]] = None,
    ) -> AgentCard:
        """Generate a card from an existing CapAuth sovereign profile.

        Reads the profile.json and public key from the CapAuth directory.

        Args:
            profile_dir: CapAuth home directory.
            transports: Contact transport endpoints.
            capabilities: Offered services.

        Returns:
            AgentCard: Card populated from the CapAuth profile.

        Raises:
            FileNotFoundError: If profile files don't exist.
        """
        base = Path(profile_dir).expanduser()
        profile_path = base / "identity" / "profile.json"
        pubkey_path = base / "identity" / "public.asc"

        if not profile_path.exists():
            raise FileNotFoundError(f"CapAuth profile not found: {profile_path}")
        if not pubkey_path.exists():
            raise FileNotFoundError(f"Public key not found: {pubkey_path}")

        profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
        public_key = pubkey_path.read_text(encoding="utf-8")

        entity = profile_data.get("entity", {})
        key_info = profile_data.get("key_info", {})

        return cls.generate(
            name=entity.get("name", "unknown"),
            fingerprint=key_info.get("fingerprint", ""),
            public_key=public_key,
            entity_type=entity.get("entity_type", "human"),
            transports=transports,
            capabilities=capabilities,
        )

    def content_hash(self) -> str:
        """Compute SHA-256 hash of the card content (excluding signature).

        Returns:
            str: Hex digest of the card content.
        """
        data = self.model_dump(exclude={"signature"})
        serialized = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode()).hexdigest()

    def sign(self, private_key_armor: str, passphrase: str) -> None:
        """Sign this card with a PGP private key.

        Sets the signature field with a PGP signature over
        the card's content hash.

        Args:
            private_key_armor: ASCII-armored PGP private key.
            passphrase: Passphrase to unlock the key.
        """
        try:
            import pgpy

            key, _ = pgpy.PGPKey.from_blob(private_key_armor)
            content = self.content_hash().encode("utf-8")
            pgp_message = pgpy.PGPMessage.new(content, cleartext=False)

            with key.unlock(passphrase):
                sig = key.sign(pgp_message)

            self.signature = str(sig)
        except Exception as exc:
            logger.error("Failed to sign agent card: %s", exc)
            raise

    @staticmethod
    def verify_signature(card: AgentCard) -> bool:
        """Verify the PGP signature on an agent card.

        Uses the public key embedded in the card to verify
        the signature over the content hash.

        Args:
            card: The agent card to verify.

        Returns:
            bool: True if the signature is valid.
        """
        if not card.signature or not card.public_key:
            return False

        try:
            import pgpy

            pub_key, _ = pgpy.PGPKey.from_blob(card.public_key)
            sig = pgpy.PGPSignature.from_blob(card.signature)

            content = card.content_hash().encode("utf-8")
            pgp_message = pgpy.PGPMessage.new(content, cleartext=False)
            pgp_message |= sig

            verification = pub_key.verify(pgp_message)
            return bool(verification)
        except Exception:
            return False

    def save(self, filepath: str | Path) -> Path:
        """Save the card to a JSON file.

        Args:
            filepath: Destination path (tilde-expanded).

        Returns:
            Path: The written file path.
        """
        path = Path(filepath).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        logger.info("Agent card saved to %s", path)
        return path

    @classmethod
    def load(cls, filepath: str | Path) -> AgentCard:
        """Load a card from a JSON file.

        Args:
            filepath: Path to the card file.

        Returns:
            AgentCard: The loaded card.

        Raises:
            FileNotFoundError: If the file doesn't exist.
        """
        path = Path(filepath).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Agent card not found: {path}")
        return cls.model_validate_json(path.read_text(encoding="utf-8"))

    def to_compact(self) -> dict:
        """Export a compact representation for display or QR codes.

        Excludes the full public key to keep the size small.

        Returns:
            dict: Compact card with essential fields only.
        """
        return {
            "name": self.name,
            "type": self.entity_type,
            "fp": self.fingerprint[:16],
            "transports": [
                {"t": t.transport, "a": t.address} for t in self.transports
            ],
            "caps": [c.name for c in self.capabilities],
            "trust": self.trust_depth,
            "motto": self.motto,
            "signed": self.signature is not None,
        }

    def summary(self) -> str:
        """Human-readable summary of the card.

        Returns:
            str: Multi-line summary string.
        """
        lines = [
            f"Agent: {self.name} ({self.entity_type})",
            f"Fingerprint: {self.fingerprint[:16]}...",
            f"Trust: depth={self.trust_depth} entangled={self.entangled}",
            f"Transports: {len(self.transports)}",
            f"Capabilities: {', '.join(c.name for c in self.capabilities) or 'none'}",
            f"Signed: {'yes' if self.signature else 'no'}",
        ]
        if self.motto:
            lines.insert(1, f'Motto: "{self.motto}"')
        return "\n".join(lines)
