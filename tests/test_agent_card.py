"""Tests for the sovereign agent card -- P2P identity discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pgpy
import pytest
from pgpy.constants import (
    HashAlgorithm,
    KeyFlags,
    PubKeyAlgorithm,
    SymmetricKeyAlgorithm,
)

from skcapstone.agent_card import AgentCapability, AgentCard, TransportEndpoint

PASSPHRASE = "test-card-key-2026"


def _generate_keypair() -> tuple[str, str]:
    """Generate a test RSA-2048 keypair."""
    key = pgpy.PGPKey.new(PubKeyAlgorithm.RSAEncryptOrSign, 2048)
    uid = pgpy.PGPUID.new("TestAgent", email="test@skworld.io")
    key.add_uid(
        uid,
        usage={KeyFlags.Sign, KeyFlags.Certify},
        hashes=[HashAlgorithm.SHA256],
        ciphers=[SymmetricKeyAlgorithm.AES256],
    )
    key.protect(PASSPHRASE, SymmetricKeyAlgorithm.AES256, HashAlgorithm.SHA256)
    return str(key), str(key.pubkey)


@pytest.fixture(scope="session")
def test_keys() -> tuple[str, str]:
    """Session-scoped test keypair."""
    return _generate_keypair()


@pytest.fixture()
def sample_card(test_keys: tuple[str, str]) -> AgentCard:
    """A basic agent card for testing."""
    _, pub = test_keys
    fp = pgpy.PGPKey.from_blob(pub)[0]
    return AgentCard.generate(
        name="Jarvis",
        fingerprint=str(fp.fingerprint).replace(" ", ""),
        public_key=pub,
        entity_type="ai",
        transports=[
            TransportEndpoint(transport="file", address="/tmp/skcomm/drop"),
            TransportEndpoint(transport="nostr", address="abc123" * 10 + "abcd"),
        ],
        capabilities=[
            AgentCapability(name="chat", description="Encrypted P2P chat"),
            AgentCapability(name="memory", description="Persistent memory"),
        ],
        trust_depth=7,
        entangled=True,
        motto="Sovereignty is non-negotiable",
    )


class TestAgentCardGeneration:
    """Tests for card generation."""

    def test_generate_basic(self, sample_card: AgentCard) -> None:
        """Happy path: generate a card with all fields."""
        assert sample_card.name == "Jarvis"
        assert sample_card.entity_type == "ai"
        assert len(sample_card.fingerprint) == 40
        assert len(sample_card.transports) == 2
        assert len(sample_card.capabilities) == 2
        assert sample_card.trust_depth == 7
        assert sample_card.entangled is True
        assert sample_card.motto == "Sovereignty is non-negotiable"

    def test_card_has_uuid_id(self, sample_card: AgentCard) -> None:
        """Card gets a UUID v4 identifier."""
        assert len(sample_card.card_id) == 36
        assert sample_card.card_id.count("-") == 4

    def test_card_has_timestamp(self, sample_card: AgentCard) -> None:
        """Card gets a UTC creation timestamp."""
        assert sample_card.created_at.tzinfo is not None

    def test_unsigned_by_default(self, sample_card: AgentCard) -> None:
        """Cards are unsigned on creation."""
        assert sample_card.signature is None


class TestCardSignVerify:
    """Tests for PGP signing and verification."""

    def test_sign_and_verify(
        self, test_keys: tuple[str, str], sample_card: AgentCard
    ) -> None:
        """Happy path: sign card then verify signature."""
        priv, _ = test_keys
        sample_card.sign(priv, PASSPHRASE)
        assert sample_card.signature is not None
        assert AgentCard.verify_signature(sample_card) is True

    def test_verify_unsigned_returns_false(self, sample_card: AgentCard) -> None:
        """Unsigned card fails verification."""
        assert AgentCard.verify_signature(sample_card) is False

    def test_tampered_card_fails_verification(
        self, test_keys: tuple[str, str], sample_card: AgentCard
    ) -> None:
        """Modifying the card after signing invalidates the signature."""
        priv, _ = test_keys
        sample_card.sign(priv, PASSPHRASE)
        sample_card.name = "EvilAgent"
        assert AgentCard.verify_signature(sample_card) is False


class TestCardPersistence:
    """Tests for save/load."""

    def test_save_and_load(self, sample_card: AgentCard, tmp_path: Path) -> None:
        """Happy path: save to file and load back."""
        filepath = tmp_path / "card.json"
        sample_card.save(filepath)

        loaded = AgentCard.load(filepath)
        assert loaded.name == sample_card.name
        assert loaded.fingerprint == sample_card.fingerprint
        assert len(loaded.transports) == 2
        assert len(loaded.capabilities) == 2

    def test_load_nonexistent_raises(self) -> None:
        """Loading a missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            AgentCard.load("/nonexistent/card.json")

    def test_save_creates_parent_dirs(self, sample_card: AgentCard, tmp_path: Path) -> None:
        """Save creates parent directories if needed."""
        filepath = tmp_path / "deep" / "nested" / "card.json"
        sample_card.save(filepath)
        assert filepath.exists()

    def test_roundtrip_preserves_signature(
        self, test_keys: tuple[str, str], sample_card: AgentCard, tmp_path: Path
    ) -> None:
        """Signed card survives save/load roundtrip and still verifies."""
        priv, _ = test_keys
        sample_card.sign(priv, PASSPHRASE)

        filepath = tmp_path / "signed-card.json"
        sample_card.save(filepath)

        loaded = AgentCard.load(filepath)
        assert AgentCard.verify_signature(loaded) is True


class TestCardExport:
    """Tests for compact export and summary."""

    def test_compact_excludes_public_key(self, sample_card: AgentCard) -> None:
        """Compact export omits the full public key."""
        compact = sample_card.to_compact()
        assert "public_key" not in compact
        assert compact["name"] == "Jarvis"
        assert compact["fp"] == sample_card.fingerprint[:16]
        assert len(compact["transports"]) == 2
        assert "chat" in compact["caps"]

    def test_summary_readable(self, sample_card: AgentCard) -> None:
        """Summary produces a human-readable string."""
        summary = sample_card.summary()
        assert "Jarvis" in summary
        assert "chat" in summary
        assert "Sovereignty" in summary

    def test_content_hash_deterministic(self, sample_card: AgentCard) -> None:
        """Content hash is the same for identical cards."""
        h1 = sample_card.content_hash()
        h2 = sample_card.content_hash()
        assert h1 == h2
        assert len(h1) == 64

    def test_content_hash_changes_on_modification(self, sample_card: AgentCard) -> None:
        """Modifying the card changes the content hash."""
        h1 = sample_card.content_hash()
        sample_card.motto = "Changed!"
        h2 = sample_card.content_hash()
        assert h1 != h2
