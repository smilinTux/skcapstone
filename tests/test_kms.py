"""Tests for SKSecurity KMS — sovereign key management.

Tests the skcapstone KMS wrapper which delegates crypto operations
to sksecurity.kms (AES-256-GCM key wrapping, HKDF-SHA256 derivation).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from skcapstone.kms import (
    KeyRecord,
    KeyStatus,
    KeyStore,
    KeyType,
    RotationEntry,
    _decrypt_at_rest,
    _derive_key,
    _encrypt_at_rest,
    _key_fingerprint,
    _key_id,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home for KMS tests."""
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

    return tmp_path


@pytest.fixture
def store(home: Path) -> KeyStore:
    """Create and initialize a KeyStore."""
    ks = KeyStore(home)
    ks.initialize()
    return ks


# ---------------------------------------------------------------------------
# Crypto helper tests
# ---------------------------------------------------------------------------


class TestCryptoHelpers:
    """Tests for low-level cryptographic helpers."""

    def test_derive_key_deterministic(self) -> None:
        """Same inputs produce same output."""
        k1 = _derive_key(b"master", b"info")
        k2 = _derive_key(b"master", b"info")
        assert k1 == k2

    def test_derive_key_different_info(self) -> None:
        """Different info strings produce different keys."""
        k1 = _derive_key(b"master", b"service-a")
        k2 = _derive_key(b"master", b"service-b")
        assert k1 != k2

    def test_derive_key_length(self) -> None:
        """Output length matches requested."""
        k16 = _derive_key(b"master", b"info", length=16)
        k64 = _derive_key(b"master", b"info", length=64)
        assert len(k16) == 16
        assert len(k64) == 64

    def test_aes_gcm_roundtrip(self) -> None:
        """Encrypt then decrypt returns original plaintext."""
        key = _derive_key(b"test", b"enc", length=32)
        plaintext = b"sovereign secrets"
        ct = _encrypt_at_rest(plaintext, key)
        assert _decrypt_at_rest(ct, key) == plaintext

    def test_aes_gcm_ciphertext_format(self) -> None:
        """AES-256-GCM output is nonce (12) + ciphertext + tag (16)."""
        key = os.urandom(32)
        plaintext = b"test data"
        ct = _encrypt_at_rest(plaintext, key)
        # nonce=12, plaintext_len=9, tag=16 → total=37
        assert len(ct) == 12 + len(plaintext) + 16

    def test_aes_gcm_wrong_key_fails(self) -> None:
        """Decrypting with wrong key raises an error."""
        key1 = _derive_key(b"key1", b"enc", length=32)
        key2 = _derive_key(b"key2", b"enc", length=32)
        ct = _encrypt_at_rest(b"data", key1)
        with pytest.raises(Exception):
            _decrypt_at_rest(ct, key2)

    def test_key_fingerprint_deterministic(self) -> None:
        """Same material produces same fingerprint."""
        fp1 = _key_fingerprint(b"material")
        fp2 = _key_fingerprint(b"material")
        assert fp1 == fp2
        assert len(fp1) == 64  # SHA-256 hex

    def test_key_id_deterministic(self) -> None:
        """Same inputs produce same key_id."""
        id1 = _key_id("label", KeyType.SERVICE)
        id2 = _key_id("label", KeyType.SERVICE)
        assert id1 == id2
        assert len(id1) == 16


# ---------------------------------------------------------------------------
# KeyStore initialization
# ---------------------------------------------------------------------------


class TestKeyStoreInit:
    """Tests for KMS initialization."""

    def test_initialize_creates_master(self, home: Path) -> None:
        """Initialize creates a master key."""
        store = KeyStore(home)
        master = store.initialize()
        assert master.key_type == KeyType.MASTER
        assert master.status == KeyStatus.ACTIVE
        assert master.label == "master"

    def test_initialize_idempotent(self, store: KeyStore) -> None:
        """Calling initialize twice returns same master."""
        m1 = store.initialize()
        m2 = store.initialize()
        assert m1.key_id == m2.key_id
        assert m1.fingerprint == m2.fingerprint

    def test_initialize_creates_directory_structure(self, home: Path) -> None:
        """KMS directories are created on init."""
        KeyStore(home).initialize()
        assert (home / "security" / "kms").is_dir()
        assert (home / "security" / "kms" / "keys").is_dir()
        assert (home / "security" / "kms" / "keystore.json").exists()

    def test_initialize_without_identity(self, tmp_path: Path) -> None:
        """KMS works with random seed when no identity exists."""
        store = KeyStore(tmp_path)
        master = store.initialize()
        assert master.key_type == KeyType.MASTER
        assert master.status == KeyStatus.ACTIVE

    def test_algorithm_uses_aes_256_gcm(self, store: KeyStore) -> None:
        """Default algorithm is HKDF-SHA256+AES-256-GCM."""
        key = store.derive_service_key("test-algo")
        assert "AES-256-GCM" in key.algorithm


# ---------------------------------------------------------------------------
# Service key derivation
# ---------------------------------------------------------------------------


class TestServiceKeys:
    """Tests for service key derivation."""

    def test_derive_service_key(self, store: KeyStore) -> None:
        """Derive a service key from master."""
        key = store.derive_service_key("api-gateway")
        assert key.key_type == KeyType.SERVICE
        assert key.label == "api-gateway"
        assert key.status == KeyStatus.ACTIVE
        assert key.parent_key_id is not None

    def test_service_key_idempotent(self, store: KeyStore) -> None:
        """Same service name returns existing key."""
        k1 = store.derive_service_key("skchat")
        k2 = store.derive_service_key("skchat")
        assert k1.key_id == k2.key_id

    def test_different_services_different_keys(self, store: KeyStore) -> None:
        """Different services produce different keys."""
        k1 = store.derive_service_key("service-a")
        k2 = store.derive_service_key("service-b")
        assert k1.fingerprint != k2.fingerprint

    def test_service_key_with_ttl(self, store: KeyStore) -> None:
        """Service key with TTL has expiry set."""
        key = store.derive_service_key("temp-service", ttl_days=30)
        assert key.expires_at is not None


# ---------------------------------------------------------------------------
# Subkey derivation
# ---------------------------------------------------------------------------


class TestSubkeys:
    """Tests for subkey derivation."""

    def test_derive_subkey_from_master(self, store: KeyStore) -> None:
        """Derive a subkey from the master key."""
        key = store.derive_subkey("delegation-a")
        assert key.key_type == KeyType.SUBKEY
        assert key.label == "delegation-a"

    def test_derive_subkey_from_service(self, store: KeyStore) -> None:
        """Derive a subkey from a service key."""
        store.derive_service_key("parent-service")
        key = store.derive_subkey("child", parent_label="parent-service")
        assert key.key_type == KeyType.SUBKEY
        assert key.parent_key_id is not None

    def test_subkey_from_missing_parent_raises(self, store: KeyStore) -> None:
        """Deriving from a nonexistent parent raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            store.derive_subkey("orphan", parent_label="nonexistent")


# ---------------------------------------------------------------------------
# Team key management
# ---------------------------------------------------------------------------


class TestTeamKeys:
    """Tests for team key creation and member management."""

    def test_create_team_key(self, store: KeyStore) -> None:
        """Create a team key with members."""
        key = store.create_team_key("dev-team", members=["opus", "lumina"])
        assert key.key_type == KeyType.TEAM
        assert key.label == "dev-team"
        assert key.members == ["opus", "lumina"]

    def test_team_key_idempotent(self, store: KeyStore) -> None:
        """Same team name returns existing key."""
        k1 = store.create_team_key("team-x")
        k2 = store.create_team_key("team-x")
        assert k1.key_id == k2.key_id

    def test_add_member(self, store: KeyStore) -> None:
        """Add a member to a team key."""
        store.create_team_key("team-y", members=["opus"])
        updated = store.add_team_member("team-y", "grok")
        assert "grok" in updated.members

    def test_add_duplicate_member_noop(self, store: KeyStore) -> None:
        """Adding an existing member is a no-op."""
        store.create_team_key("team-z", members=["opus"])
        updated = store.add_team_member("team-z", "opus")
        assert updated.members.count("opus") == 1

    def test_remove_member(self, store: KeyStore) -> None:
        """Remove a member from a team key."""
        store.create_team_key("team-w", members=["opus", "lumina", "grok"])
        updated = store.remove_team_member("team-w", "lumina")
        assert "lumina" not in updated.members
        assert "opus" in updated.members

    def test_add_member_missing_team_raises(self, store: KeyStore) -> None:
        """Adding to a nonexistent team raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            store.add_team_member("ghost-team", "opus")

    def test_team_acl_grants_access(self, store: KeyStore) -> None:
        """Team member can access key material."""
        key = store.create_team_key("acl-team", members=["opus"])
        material = store.get_key_material(key.key_id, agent_name="opus")
        assert len(material) == 32

    def test_team_acl_denies_access(self, store: KeyStore) -> None:
        """Non-member is denied access to team key material."""
        key = store.create_team_key("private-team", members=["opus"])
        with pytest.raises(PermissionError, match="not in team"):
            store.get_key_material(key.key_id, agent_name="intruder")


# ---------------------------------------------------------------------------
# Key rotation
# ---------------------------------------------------------------------------


class TestKeyRotation:
    """Tests for key rotation."""

    def test_rotate_service_key(self, store: KeyStore) -> None:
        """Rotating a service key produces a new version."""
        old = store.derive_service_key("rotating-svc")
        new = store.rotate_key(old.key_id, reason="scheduled")
        assert new.version == old.version + 1
        assert new.fingerprint != old.fingerprint
        assert new.status == KeyStatus.ACTIVE

    def test_old_key_marked_rotated(self, store: KeyStore) -> None:
        """The old key is marked as ROTATED after rotation."""
        old = store.derive_service_key("rotate-me")
        old_id = old.key_id
        store.rotate_key(old_id)

        records = store.list_keys(include_inactive=True)
        old_record = next(r for r in records if r.key_id == old_id)
        assert old_record.status == KeyStatus.ROTATED
        assert old_record.rotated_at is not None

    def test_rotate_team_key_preserves_members(self, store: KeyStore) -> None:
        """Team key rotation preserves the member list."""
        old = store.create_team_key("team-rotate", members=["opus", "grok"])
        new = store.rotate_key(old.key_id)
        assert new.members == ["opus", "grok"]

    def test_rotation_log_written(self, store: KeyStore, home: Path) -> None:
        """Rotation events are logged."""
        old = store.derive_service_key("log-svc")
        store.rotate_key(old.key_id)

        log_file = home / "security" / "kms" / "rotation-log.json"
        assert log_file.exists()
        log = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(log) >= 1
        assert log[-1]["old_version"] == 1
        assert log[-1]["new_version"] == 2

    def test_rotate_master_key(self, store: KeyStore) -> None:
        """Master key can be rotated."""
        keys = store.list_keys(key_type=KeyType.MASTER)
        master = keys[0]
        new = store.rotate_key(master.key_id, reason="security audit")
        assert new.version == master.version + 1

    def test_rotate_missing_key_raises(self, store: KeyStore) -> None:
        """Rotating a nonexistent key raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            store.rotate_key("nonexistent-id")


# ---------------------------------------------------------------------------
# Key revocation
# ---------------------------------------------------------------------------


class TestKeyRevocation:
    """Tests for key revocation."""

    def test_revoke_key(self, store: KeyStore) -> None:
        """Revoking a key marks it and removes material."""
        key = store.derive_service_key("revoke-me")
        revoked = store.revoke_key(key.key_id)
        assert revoked.status == KeyStatus.REVOKED

    def test_revoked_key_material_deleted(self, store: KeyStore, home: Path) -> None:
        """Key material is deleted on revocation."""
        key = store.derive_service_key("delete-material")
        key_file = home / "security" / "kms" / "keys" / f"{key.key_id}.key.enc"
        assert key_file.exists()
        store.revoke_key(key.key_id)
        assert not key_file.exists()

    def test_revoked_key_not_in_active_list(self, store: KeyStore) -> None:
        """Revoked keys don't appear in active listing."""
        key = store.derive_service_key("hidden")
        store.revoke_key(key.key_id)
        active = store.list_keys()
        assert all(r.key_id != key.key_id for r in active)


# ---------------------------------------------------------------------------
# Listing and status
# ---------------------------------------------------------------------------


class TestListingAndStatus:
    """Tests for key listing and status reporting."""

    def test_list_keys_by_type(self, store: KeyStore) -> None:
        """Filter keys by type."""
        store.derive_service_key("svc-1")
        store.create_team_key("team-1")
        services = store.list_keys(key_type=KeyType.SERVICE)
        teams = store.list_keys(key_type=KeyType.TEAM)
        assert all(r.key_type == KeyType.SERVICE for r in services)
        assert all(r.key_type == KeyType.TEAM for r in teams)

    def test_status_summary(self, store: KeyStore) -> None:
        """Status returns structured summary."""
        store.derive_service_key("s1")
        store.create_team_key("t1", members=["opus"])
        status = store.status()
        assert status["initialized"] is True
        assert status["active"] >= 3  # master + service + team
        assert "service" in status["by_type"]
        assert "team" in status["by_type"]
        assert "backend_available" in status

    def test_get_key_material(self, store: KeyStore) -> None:
        """Raw key material can be retrieved."""
        key = store.derive_service_key("get-material")
        material = store.get_key_material(key.key_id)
        assert len(material) == 32

    def test_get_key_material_revoked_raises(self, store: KeyStore) -> None:
        """Cannot get material for revoked key."""
        key = store.derive_service_key("revoked-access")
        store.revoke_key(key.key_id)
        with pytest.raises(ValueError, match="revoked"):
            store.get_key_material(key.key_id)


# ---------------------------------------------------------------------------
# Backend integration tests
# ---------------------------------------------------------------------------


class TestBackendIntegration:
    """Tests for sksecurity KMS backend integration."""

    def test_backend_property_available(self, store: KeyStore) -> None:
        """Backend property returns KMS when sksecurity is installed."""
        from skcapstone.kms import _HAS_BACKEND
        if _HAS_BACKEND:
            assert store.backend is not None
            assert store.backend.is_unsealed
        else:
            assert store.backend is None

    def test_status_reports_backend(self, store: KeyStore) -> None:
        """Status includes backend availability information."""
        status = store.status()
        assert "backend_available" in status
        assert "backend_unsealed" in status

    def test_backend_4_tier_operations(self, store: KeyStore) -> None:
        """When backend is available, 4-tier operations work."""
        from skcapstone.kms import _HAS_BACKEND
        if not _HAS_BACKEND:
            pytest.skip("sksecurity not installed")

        backend = store.backend
        team_key = backend.create_team_key("backend-team")
        assert team_key.team_id == "backend-team"

        agent_key = backend.create_agent_key("backend-team", "agent-01")
        assert agent_key.agent_id == "agent-01"

        dek = backend.create_dek("backend-team", "agent-01", purpose="test")
        raw_dek = backend.unwrap_dek(dek.key_id)
        assert len(raw_dek) == 32


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for Pydantic models."""

    def test_key_record_defaults(self) -> None:
        """KeyRecord has sensible defaults."""
        record = KeyRecord(
            key_id="test123",
            key_type=KeyType.SERVICE,
            label="test",
            fingerprint="abc" * 20,
        )
        assert record.status == KeyStatus.ACTIVE
        assert record.version == 1
        assert record.members == []

    def test_key_record_default_algorithm(self) -> None:
        """KeyRecord default algorithm is AES-256-GCM based."""
        record = KeyRecord(
            key_id="test",
            key_type=KeyType.SERVICE,
            label="test",
            fingerprint="abc",
        )
        assert "AES-256-GCM" in record.algorithm

    def test_rotation_entry_serializes(self) -> None:
        """RotationEntry can be serialized to JSON."""
        entry = RotationEntry(
            key_id="k1",
            old_fingerprint="old",
            new_fingerprint="new",
            old_version=1,
            new_version=2,
        )
        data = entry.model_dump(mode="json")
        assert data["old_version"] == 1
        assert data["new_version"] == 2
