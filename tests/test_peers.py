"""Tests for the skcapstone peer management module.

Covers:
- add_peer_from_card (success, missing file, invalid JSON, missing name)
- add_peer_manual (with/without key)
- list_peers (empty, with peers)
- get_peer / remove_peer
- PeerRecord model
- SKComm peer file creation
- CLI commands (add, list, remove, show)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.peers import (
    PeerRecord,
    add_peer_from_card,
    add_peer_manual,
    get_peer,
    list_peers,
    remove_peer,
)


@pytest.fixture
def homes(tmp_path):
    """Create skcapstone and skcomm home directories."""
    sk = tmp_path / ".skcapstone"
    sc = tmp_path / ".skcomm"
    sk.mkdir()
    sc.mkdir()
    return sk, sc


@pytest.fixture
def card_file(tmp_path):
    """Create a sample identity card file."""
    card = {
        "skcapstone_card": "1.0.0",
        "name": "Lumina",
        "fingerprint": "AABB1122CCDD3344EEFF5566",
        "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfakekey\n-----END PGP PUBLIC KEY BLOCK-----",
        "entity_type": "ai",
        "handle": "lumina@skworld.io",
        "email": "lumina@skworld.io",
        "capabilities": ["capauth:identity", "skchat:p2p-chat"],
        "contact_uris": ["capauth:AABB1122CCDD3344"],
    }
    path = tmp_path / "lumina-card.json"
    path.write_text(json.dumps(card, indent=2))
    return path


class TestPeerRecord:
    """Test PeerRecord model."""

    def test_defaults(self):
        """Record has sensible defaults."""
        p = PeerRecord(name="Test")
        assert p.name == "Test"
        assert p.trust_level == "unknown"
        assert p.source == "manual"
        assert p.added_at != ""

    def test_serialization(self):
        """Record round-trips through JSON."""
        p = PeerRecord(
            name="Opus",
            fingerprint="FP123",
            capabilities=["capauth:identity"],
        )
        data = json.loads(p.model_dump_json())
        restored = PeerRecord.model_validate(data)
        assert restored.name == "Opus"
        assert restored.fingerprint == "FP123"


class TestAddPeerFromCard:
    """Test importing peers from identity cards."""

    def test_add_from_card(self, card_file, homes):
        """Card import creates peer records in both registries."""
        sk, sc = homes
        peer = add_peer_from_card(card_file, skcapstone_home=sk, skcomm_home=sc)

        assert peer.name == "Lumina"
        assert peer.fingerprint == "AABB1122CCDD3344EEFF5566"
        assert peer.entity_type == "ai"
        assert peer.trust_level == "verified"
        assert peer.source == "card"
        assert "capauth:identity" in peer.capabilities

        assert (sk / "peers" / "lumina.json").exists()
        assert (sc / "peers" / "lumina.yml").exists()
        assert (sc / "peers" / "lumina.pub.asc").exists()

    def test_missing_card_raises(self, homes):
        """Nonexistent card raises FileNotFoundError."""
        sk, sc = homes
        with pytest.raises(FileNotFoundError):
            add_peer_from_card(Path("/nope.json"), skcapstone_home=sk, skcomm_home=sc)

    def test_invalid_json_raises(self, tmp_path, homes):
        """Invalid JSON raises ValueError."""
        sk, sc = homes
        bad = tmp_path / "bad.json"
        bad.write_text("{{{not json")
        with pytest.raises(ValueError):
            add_peer_from_card(bad, skcapstone_home=sk, skcomm_home=sc)

    def test_missing_name_raises(self, tmp_path, homes):
        """Card without name raises ValueError."""
        sk, sc = homes
        no_name = tmp_path / "noname.json"
        no_name.write_text(json.dumps({"fingerprint": "123"}))
        with pytest.raises(ValueError, match="name"):
            add_peer_from_card(no_name, skcapstone_home=sk, skcomm_home=sc)


class TestAddPeerManual:
    """Test manual peer creation."""

    def test_add_manual_basic(self, homes):
        """Manual add creates a peer record."""
        sk, sc = homes
        peer = add_peer_manual(
            name="Opus", email="opus@smilintux.org",
            skcapstone_home=sk, skcomm_home=sc,
        )
        assert peer.name == "Opus"
        assert peer.email == "opus@smilintux.org"
        assert (sk / "peers" / "opus.json").exists()

    def test_add_manual_with_key(self, tmp_path, homes):
        """Manual add with public key file imports the key."""
        sk, sc = homes
        key_file = tmp_path / "opus.pub.asc"
        key_file.write_text("-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----")

        peer = add_peer_manual(
            name="Opus", public_key_path=key_file,
            skcapstone_home=sk, skcomm_home=sc,
        )
        assert peer.public_key != ""
        assert peer.trust_level == "verified"
        assert (sc / "peers" / "opus.pub.asc").exists()


class TestListPeers:
    """Test peer listing."""

    def test_empty_list(self, homes):
        """No peers returns empty list."""
        sk, _ = homes
        assert list_peers(skcapstone_home=sk) == []

    def test_list_with_peers(self, card_file, homes):
        """Added peers appear in listing."""
        sk, sc = homes
        add_peer_from_card(card_file, skcapstone_home=sk, skcomm_home=sc)

        peers = list_peers(skcapstone_home=sk)
        assert len(peers) == 1
        assert peers[0].name == "Lumina"


class TestGetPeer:
    """Test single peer lookup."""

    def test_get_existing(self, card_file, homes):
        """Known peer is returned."""
        sk, sc = homes
        add_peer_from_card(card_file, skcapstone_home=sk, skcomm_home=sc)

        peer = get_peer("Lumina", skcapstone_home=sk)
        assert peer is not None
        assert peer.name == "Lumina"

    def test_get_unknown(self, homes):
        """Unknown peer returns None."""
        sk, _ = homes
        assert get_peer("Nobody", skcapstone_home=sk) is None


class TestRemovePeer:
    """Test peer removal."""

    def test_remove_existing(self, card_file, homes):
        """Removing an existing peer cleans up all files."""
        sk, sc = homes
        add_peer_from_card(card_file, skcapstone_home=sk, skcomm_home=sc)

        assert remove_peer("Lumina", skcapstone_home=sk, skcomm_home=sc)
        assert not (sk / "peers" / "lumina.json").exists()
        assert not (sc / "peers" / "lumina.yml").exists()

    def test_remove_unknown(self, homes):
        """Removing unknown peer returns False."""
        sk, sc = homes
        assert not remove_peer("Nobody", skcapstone_home=sk, skcomm_home=sc)


class TestCLI:
    """Test peer CLI commands."""

    def test_peer_help(self):
        """peer --help works."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["peer", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "show" in result.output

    def test_peer_list_empty(self, homes):
        """peer list on empty registry shows message."""
        from skcapstone.cli import main
        sk, _ = homes
        runner = CliRunner()
        result = runner.invoke(main, ["peer", "list", "--home", str(sk)])
        assert result.exit_code == 0
        assert "No peers" in result.output

    def test_peer_add_from_card_cli(self, card_file, homes):
        """peer add --card via CLI."""
        from skcapstone.cli import main
        sk, _ = homes
        runner = CliRunner()
        result = runner.invoke(main, [
            "peer", "add", "--card", str(card_file), "--home", str(sk),
        ])
        assert result.exit_code == 0
        assert "Lumina" in result.output

    def test_peer_add_no_args(self):
        """peer add without args shows usage hint."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["peer", "add"])
        assert result.exit_code == 1
