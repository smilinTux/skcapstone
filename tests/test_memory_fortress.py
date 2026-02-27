"""Tests for Memory Fortress — integrity sealing, encryption, tamper alerts."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.memory_fortress import (
    MemoryFortress,
    FortressConfig,
    SealResult,
    _SEAL_FIELD,
    _SEALED_AT_FIELD,
    _ENCRYPTED_FIELD,
)
from skcapstone.models import MemoryEntry, MemoryLayer


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home for fortress tests."""
    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    manifest = {
        "name": "test-agent",
        "email": "test@skcapstone.local",
        "fingerprint": "ABCD1234567890ABCDEF1234567890ABCDEF1234",
        "capauth_managed": False,
    }
    (identity_dir / "identity.json").write_text(json.dumps(manifest), encoding="utf-8")

    security_dir = tmp_path / "security"
    security_dir.mkdir()

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    for layer in ("short-term", "mid-term", "long-term"):
        (mem_dir / layer).mkdir()

    return tmp_path


@pytest.fixture
def seal_key() -> bytes:
    """A deterministic seal key for tests."""
    return b"test-fortress-seal-key-32-bytes!!"


@pytest.fixture
def fortress(home: Path, seal_key: bytes) -> MemoryFortress:
    """Create an initialized MemoryFortress with explicit seal key."""
    f = MemoryFortress(home, seal_key=seal_key)
    f.initialize()
    return f


@pytest.fixture
def sample_entry() -> MemoryEntry:
    """A sample memory entry for testing."""
    return MemoryEntry(
        memory_id="abc123def456",
        content="The sovereign agent remembers everything.",
        tags=["test", "fortress"],
        source="test",
        layer=MemoryLayer.SHORT_TERM,
        importance=0.8,
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for fortress setup."""

    def test_initialize_creates_config(self, home: Path, seal_key: bytes) -> None:
        """Initialize creates fortress.json config."""
        f = MemoryFortress(home, seal_key=seal_key)
        config = f.initialize()
        assert config.enabled is True
        assert (home / "memory" / "fortress.json").exists()

    def test_initialize_idempotent(self, fortress: MemoryFortress) -> None:
        """Multiple initializations don't break anything."""
        c1 = fortress.initialize()
        c2 = fortress.initialize()
        assert c1.seal_algorithm == c2.seal_algorithm

    def test_initialize_with_encryption(self, home: Path, seal_key: bytes) -> None:
        """Initialize with encryption enabled creates config."""
        f = MemoryFortress(home, seal_key=seal_key, encryption_enabled=True)
        config = f.initialize()
        assert config.encryption_enabled is True


# ---------------------------------------------------------------------------
# Sealing
# ---------------------------------------------------------------------------


class TestSealing:
    """Tests for integrity seal operations."""

    def test_seal_entry_adds_seal_field(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry,
    ) -> None:
        """Sealing adds the __fortress_seal field."""
        data = fortress.seal_entry(sample_entry)
        assert _SEAL_FIELD in data
        assert _SEALED_AT_FIELD in data

    def test_seal_is_deterministic(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry,
    ) -> None:
        """Same entry produces same seal (excluding timestamp)."""
        data1 = fortress.seal_entry(sample_entry)
        data2 = fortress.seal_entry(sample_entry)
        # Seals match when the content is identical (timestamp excluded from seal)
        # The seal covers the data dict which includes created_at etc.
        assert data1[_SEAL_FIELD] == data2[_SEAL_FIELD]

    def test_seal_changes_with_content(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry,
    ) -> None:
        """Different content produces different seal."""
        data1 = fortress.seal_entry(sample_entry)
        sample_entry.content = "Something completely different"
        data2 = fortress.seal_entry(sample_entry)
        assert data1[_SEAL_FIELD] != data2[_SEAL_FIELD]

    def test_seal_is_hex_string(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry,
    ) -> None:
        """Seal is a 64-char hex string (SHA-256)."""
        data = fortress.seal_entry(sample_entry)
        seal = data[_SEAL_FIELD]
        assert len(seal) == 64
        assert all(c in "0123456789abcdef" for c in seal)


# ---------------------------------------------------------------------------
# Verify and Load
# ---------------------------------------------------------------------------


class TestVerifyAndLoad:
    """Tests for integrity verification on load."""

    def test_verify_sealed_entry(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Sealed entry passes verification."""
        path = fortress.save_sealed(home, sample_entry)
        entry, result = fortress.verify_and_load(path)
        assert entry is not None
        assert result.verified is True
        assert result.tampered is False
        assert entry.content == sample_entry.content

    def test_detect_tampering(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Modified content is detected as tampering."""
        path = fortress.save_sealed(home, sample_entry)

        # Tamper with the file
        data = json.loads(path.read_text(encoding="utf-8"))
        data["content"] = "I have been tampered with!"
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        entry, result = fortress.verify_and_load(path)
        assert entry is None
        assert result.tampered is True
        assert result.verified is False
        assert "tampering" in result.error.lower()

    def test_detect_tag_tampering(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Modified tags are detected as tampering."""
        path = fortress.save_sealed(home, sample_entry)

        data = json.loads(path.read_text(encoding="utf-8"))
        data["tags"] = ["hacked"]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        entry, result = fortress.verify_and_load(path)
        assert result.tampered is True

    def test_detect_importance_tampering(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Modified importance score is detected."""
        path = fortress.save_sealed(home, sample_entry)

        data = json.loads(path.read_text(encoding="utf-8"))
        data["importance"] = 1.0
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        entry, result = fortress.verify_and_load(path)
        assert result.tampered is True

    def test_legacy_unsealed_memory(self, fortress: MemoryFortress, home: Path) -> None:
        """Legacy memories without seals load with verified=None."""
        entry = MemoryEntry(
            memory_id="legacy123",
            content="Old memory without seal",
            tags=["legacy"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
        )
        path = home / "memory" / "short-term" / "legacy123.json"
        path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")

        loaded, result = fortress.verify_and_load(path)
        assert loaded is not None
        assert result.sealed is False
        assert result.verified is None
        assert result.tampered is False

    def test_corrupt_json_file(self, fortress: MemoryFortress, home: Path) -> None:
        """Corrupt JSON file returns error result."""
        path = home / "memory" / "short-term" / "corrupt.json"
        path.write_text("not valid json {{{", encoding="utf-8")

        entry, result = fortress.verify_and_load(path)
        assert entry is None
        assert result.error is not None


# ---------------------------------------------------------------------------
# Save Sealed
# ---------------------------------------------------------------------------


class TestSaveSealed:
    """Tests for atomic sealed writes."""

    def test_save_creates_file(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Save creates the sealed JSON file."""
        path = fortress.save_sealed(home, sample_entry)
        assert path.exists()

    def test_saved_file_contains_seal(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Saved file contains the fortress seal."""
        path = fortress.save_sealed(home, sample_entry)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert _SEAL_FIELD in data
        assert _SEALED_AT_FIELD in data

    def test_roundtrip_save_load(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Save then load returns identical entry."""
        path = fortress.save_sealed(home, sample_entry)
        loaded, result = fortress.verify_and_load(path)
        assert loaded is not None
        assert loaded.memory_id == sample_entry.memory_id
        assert loaded.content == sample_entry.content
        assert loaded.tags == sample_entry.tags

    def test_no_tmp_file_left(
        self, fortress: MemoryFortress, sample_entry: MemoryEntry, home: Path,
    ) -> None:
        """Atomic write leaves no .tmp file."""
        path = fortress.save_sealed(home, sample_entry)
        tmp = path.with_suffix(".json.tmp")
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# Verify All
# ---------------------------------------------------------------------------


class TestVerifyAll:
    """Tests for full memory scan."""

    def test_verify_all_empty(self, fortress: MemoryFortress, home: Path) -> None:
        """Verify all on empty memory returns empty."""
        results = fortress.verify_all(home)
        assert results == []

    def test_verify_all_sealed(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Verify all with sealed memories passes."""
        for i in range(3):
            entry = MemoryEntry(
                memory_id=f"mem{i:03d}",
                content=f"Memory number {i}",
                tags=["batch"],
                source="test",
                layer=MemoryLayer.SHORT_TERM,
            )
            fortress.save_sealed(home, entry)

        results = fortress.verify_all(home)
        assert len(results) == 3
        assert all(r.verified is True for r in results)

    def test_verify_all_detects_tampering(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Verify all detects tampered memories in batch."""
        for i in range(3):
            entry = MemoryEntry(
                memory_id=f"scan{i:03d}",
                content=f"Memory {i}",
                tags=["scan"],
                source="test",
                layer=MemoryLayer.SHORT_TERM,
            )
            fortress.save_sealed(home, entry)

        # Tamper with one
        tampered_path = home / "memory" / "short-term" / "scan001.json"
        data = json.loads(tampered_path.read_text(encoding="utf-8"))
        data["content"] = "HACKED"
        tampered_path.write_text(json.dumps(data), encoding="utf-8")

        results = fortress.verify_all(home)
        tampered = [r for r in results if r.tampered]
        verified = [r for r in results if r.verified]
        assert len(tampered) == 1
        assert len(verified) == 2


# ---------------------------------------------------------------------------
# Seal Existing (Migration)
# ---------------------------------------------------------------------------


class TestSealExisting:
    """Tests for migrating legacy memories."""

    def test_seal_existing_memories(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Seal existing unsealed memories."""
        for i in range(3):
            entry = MemoryEntry(
                memory_id=f"old{i:03d}",
                content=f"Unsealed memory {i}",
                tags=["legacy"],
                source="test",
                layer=MemoryLayer.MID_TERM,
            )
            path = home / "memory" / "mid-term" / f"old{i:03d}.json"
            path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")

        sealed_count = fortress.seal_existing(home)
        assert sealed_count == 3

        # Verify they're now sealed
        results = fortress.verify_all(home)
        assert all(r.verified is True for r in results)

    def test_seal_existing_skips_already_sealed(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Already-sealed memories are not re-sealed."""
        entry = MemoryEntry(
            memory_id="alreadysealed",
            content="Already protected",
            tags=["sealed"],
            source="test",
            layer=MemoryLayer.LONG_TERM,
        )
        fortress.save_sealed(home, entry)

        sealed_count = fortress.seal_existing(home)
        assert sealed_count == 0

    def test_seal_existing_empty(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """No memories to seal returns 0."""
        assert fortress.seal_existing(home) == 0


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestEncryption:
    """Tests for at-rest encryption."""

    def test_encrypted_roundtrip(self, home: Path) -> None:
        """Encrypt then decrypt returns original content."""
        fortress = MemoryFortress(home, encryption_enabled=True)
        fortress.initialize()

        entry = MemoryEntry(
            memory_id="encrypted01",
            content="Top secret sovereign data",
            tags=["encrypted"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
            importance=0.9,
        )

        path = fortress.save_sealed(home, entry)
        loaded, result = fortress.verify_and_load(path)
        assert loaded is not None
        assert loaded.content == "Top secret sovereign data"
        assert result.verified is True

    def test_encrypted_content_not_plaintext(self, home: Path) -> None:
        """Encrypted file does not contain plaintext content."""
        fortress = MemoryFortress(home, encryption_enabled=True)
        fortress.initialize()

        entry = MemoryEntry(
            memory_id="hidden01",
            content="This text should be encrypted on disk",
            tags=["secret"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
        )

        path = fortress.save_sealed(home, entry)
        raw = path.read_text(encoding="utf-8")
        assert "This text should be encrypted on disk" not in raw
        assert _ENCRYPTED_FIELD in raw


# ---------------------------------------------------------------------------
# Different Seal Keys
# ---------------------------------------------------------------------------


class TestSealKeys:
    """Tests for seal key behavior."""

    def test_different_keys_different_seals(
        self, home: Path, sample_entry: MemoryEntry,
    ) -> None:
        """Different seal keys produce different seals."""
        f1 = MemoryFortress(home, seal_key=b"key-one-32-bytes-padded-here!!!!")
        f1.initialize()
        f2 = MemoryFortress(home, seal_key=b"key-two-32-bytes-padded-here!!!!")
        f2.initialize()

        data1 = f1.seal_entry(sample_entry)
        data2 = f2.seal_entry(sample_entry)
        assert data1[_SEAL_FIELD] != data2[_SEAL_FIELD]

    def test_wrong_key_detects_tampering(
        self, home: Path, sample_entry: MemoryEntry,
    ) -> None:
        """Loading with wrong seal key triggers tamper alert."""
        f1 = MemoryFortress(home, seal_key=b"original-key-32-bytes-padded!!!")
        f1.initialize()
        path = f1.save_sealed(home, sample_entry)

        f2 = MemoryFortress(home, seal_key=b"different-key-32-bytes-padded!!")
        f2.initialize()
        _, result = f2.verify_and_load(path)
        assert result.tampered is True


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


class TestStatus:
    """Tests for fortress status reporting."""

    def test_status_returns_config(self, fortress: MemoryFortress) -> None:
        """Status returns current config info."""
        status = fortress.status()
        assert status["enabled"] is True
        assert status["seal_algorithm"] == "hmac-sha256"
        assert status["has_seal_key"] is True

    def test_status_reflects_encryption(self, home: Path) -> None:
        """Status reflects encryption setting."""
        f = MemoryFortress(home, seal_key=b"k" * 32, encryption_enabled=True)
        f.initialize()
        status = f.status()
        assert status["encryption_enabled"] is True


# ---------------------------------------------------------------------------
# Audit Trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    """Tests for security audit integration."""

    def test_init_writes_audit(
        self, home: Path, seal_key: bytes,
    ) -> None:
        """Initialization writes an audit event."""
        f = MemoryFortress(home, seal_key=seal_key)
        f.initialize()

        audit_log = home / "security" / "audit.log"
        assert audit_log.exists()
        content = audit_log.read_text(encoding="utf-8")
        assert "FORTRESS_INIT" in content

    def test_tamper_writes_audit(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Tamper detection writes audit event."""
        entry = MemoryEntry(
            memory_id="audittamp",
            content="Watch me",
            tags=["audit"],
            source="test",
            layer=MemoryLayer.SHORT_TERM,
        )
        path = fortress.save_sealed(home, entry)

        # Tamper
        data = json.loads(path.read_text(encoding="utf-8"))
        data["content"] = "EVIL"
        path.write_text(json.dumps(data), encoding="utf-8")

        fortress.verify_and_load(path)

        audit_log = home / "security" / "audit.log"
        content = audit_log.read_text(encoding="utf-8")
        assert "MEMORY_TAMPER_ALERT" in content

    def test_scan_writes_audit(
        self, fortress: MemoryFortress, home: Path,
    ) -> None:
        """Full scan writes audit event."""
        fortress.verify_all(home)

        audit_log = home / "security" / "audit.log"
        content = audit_log.read_text(encoding="utf-8")
        assert "FORTRESS_SCAN" in content


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for fortress models."""

    def test_fortress_config_defaults(self) -> None:
        """FortressConfig has sensible defaults."""
        config = FortressConfig()
        assert config.enabled is True
        assert config.encryption_enabled is False
        assert config.seal_algorithm == "hmac-sha256"

    def test_seal_result_defaults(self) -> None:
        """SealResult has sensible defaults."""
        result = SealResult(memory_id="test", sealed=True)
        assert result.tampered is False
        assert result.error is None
        assert result.verified is None
