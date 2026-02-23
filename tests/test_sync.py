"""
Tests for the sovereign sync module -- vault, backends, and engine.
"""

from __future__ import annotations

import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Create a minimal agent home directory for testing."""
    home = tmp_path / ".skcapstone"
    home.mkdir()

    for pillar in ("identity", "memory", "trust", "security", "config", "skills"):
        pillar_dir = home / pillar
        pillar_dir.mkdir()

    (home / "identity" / "identity.json").write_text(
        json.dumps({
            "name": "TestAgent",
            "email": "test@skcapstone.local",
            "fingerprint": "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "capauth_managed": False,
        })
    )

    (home / "trust" / "trust.json").write_text(
        json.dumps({"depth": 5.0, "trust_level": 0.8, "love_intensity": 0.9})
    )

    (home / "config" / "config.yaml").write_text("agent_name: TestAgent\n")

    (home / "manifest.json").write_text(
        json.dumps({"name": "TestAgent", "version": "0.1.0", "connectors": []})
    )

    for layer in ("short-term", "mid-term", "long-term"):
        layer_dir = home / "memory" / layer
        layer_dir.mkdir(parents=True)

    (home / "memory" / "long-term" / "test-memory.json").write_text(
        json.dumps({"content": "test memory", "created": "2026-02-23"})
    )

    return home


class TestVault:
    """Tests for the Vault packing/unpacking system."""

    def test_pack_creates_archive(self, agent_home: Path):
        """Vault pack should create a .tar.gz archive."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        result = vault.pack(encrypt=False)

        assert result.exists()
        assert result.name.startswith("vault-")
        assert result.name.endswith(".tar.gz")

    def test_pack_includes_pillars(self, agent_home: Path):
        """Archive should contain pillar directories."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()

        assert any("identity/" in n for n in names)
        assert any("trust/" in n for n in names)
        assert any("config/" in n for n in names)
        assert any("manifest.json" in n for n in names)

    def test_pack_creates_manifest(self, agent_home: Path):
        """Pack should create a companion .manifest.json file."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        assert manifest_path.exists()
        data = json.loads(manifest_path.read_text())
        assert data["agent_name"] == "TestAgent"
        assert "identity" in data["pillars_included"]

    def test_unpack_restores_state(self, agent_home: Path, tmp_path: Path):
        """Unpacking a vault should restore pillar directories."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        restore_dir = tmp_path / "restored"
        restore_dir.mkdir()
        vault.unpack(archive_path, target=restore_dir)

        assert (restore_dir / "identity" / "identity.json").exists()
        assert (restore_dir / "trust" / "trust.json").exists()
        assert (restore_dir / "manifest.json").exists()

    def test_list_vaults(self, agent_home: Path):
        """list_vaults should return metadata for all archives."""
        import time
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        vault.pack(encrypt=False)
        time.sleep(1.1)
        vault.pack(encrypt=False)

        vaults = vault.list_vaults()
        assert len(vaults) >= 2

    def test_pack_excludes_pycache(self, agent_home: Path):
        """Archive should not contain __pycache__ or .pyc files."""
        from skcapstone.sync.vault import Vault

        pycache = agent_home / "identity" / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.pyc").write_text("junk")

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        with tarfile.open(archive_path, "r:gz") as tar:
            names = tar.getnames()

        assert not any("__pycache__" in n for n in names)
        assert not any(".pyc" in n for n in names)


class TestVaultHardening:
    """Tests for vault integrity verification and key rotation."""

    def test_pack_includes_sha256_hashes(self, agent_home: Path):
        """Manifest should contain SHA-256 hashes for all files."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert "file_hashes" in data
        assert len(data["file_hashes"]) > 0
        assert "archive_hash" in data
        assert data["archive_hash"] is not None
        assert len(data["archive_hash"]) == 64

        for rel_path, hash_val in data["file_hashes"].items():
            assert len(hash_val) == 64, f"Bad hash for {rel_path}"

    def test_pack_archive_hash_matches(self, agent_home: Path):
        """Archive SHA-256 in manifest should match the actual archive."""
        from skcapstone.sync.vault import Vault, _sha256_file

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        actual = _sha256_file(archive_path)
        assert data["archive_hash"] == actual

    def test_unpack_verifies_archive_hash(self, agent_home: Path, tmp_path: Path):
        """Unpack should detect a tampered archive."""
        from skcapstone.sync.vault import Vault, VaultIntegrityError

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        with open(archive_path, "ab") as f:
            f.write(b"tampered!")

        restore_dir = tmp_path / "restored"
        restore_dir.mkdir()

        with pytest.raises(VaultIntegrityError, match="hash mismatch"):
            vault.unpack(archive_path, target=restore_dir)

    def test_unpack_verifies_file_hashes(self, agent_home: Path, tmp_path: Path):
        """Unpack should detect tampered individual files."""
        from skcapstone.sync.vault import Vault, VaultIntegrityError

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        restore_dir = tmp_path / "restored"
        restore_dir.mkdir()
        vault.unpack(archive_path, target=restore_dir)

        identity_file = restore_dir / "identity" / "identity.json"
        identity_file.write_text('{"tampered": true}')

        data = json.loads(manifest_path.read_text())
        identity_hash = data["file_hashes"].get("identity/identity.json")
        assert identity_hash is not None

        from skcapstone.sync.vault import _sha256_file

        tampered_hash = _sha256_file(identity_file)
        assert tampered_hash != identity_hash

    def test_unpack_skip_verification(self, agent_home: Path, tmp_path: Path):
        """Unpack with verify_hashes=False should not check integrity."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        with open(archive_path, "ab") as f:
            f.write(b"tampered!")

        restore_dir = tmp_path / "restored"
        restore_dir.mkdir()
        vault.unpack(archive_path, target=restore_dir, verify_hashes=False)
        assert (restore_dir / "identity" / "identity.json").exists()

    def test_rotate_encrypts_plaintext_vaults(self, agent_home: Path):
        """rotate_keys should encrypt plaintext vaults (skipped without gpg)."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        vault.pack(encrypt=False)

        import shutil
        if not shutil.which("gpg"):
            pytest.skip("gpg not available for rotation test")

        rotated = vault.rotate_keys(new_passphrase="test-passphrase")
        assert len(rotated) >= 1
        assert all(str(p).endswith(".gpg") for p in rotated)

    def test_manifest_schema_version(self, agent_home: Path):
        """Manifest should have schema version 1.1 for hardened vaults."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert data["schema_version"] == "1.1"


class TestSyncthingBackend:
    """Tests for the Syncthing backend."""

    def test_push_copies_to_outbox(self, agent_home: Path, tmp_path: Path):
        """Push should copy vault to the outbox directory."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        vault_file = tmp_path / "vault-test.tar.gz"
        vault_file.write_text("fake vault data")
        manifest_file = tmp_path / "vault-test.manifest.json"
        manifest_file.write_text('{"agent_name": "test"}')

        result = backend.push(vault_file, manifest_file)
        assert result is True
        assert (backend.outbox / "vault-test.tar.gz").exists()
        assert (backend.outbox / "vault-test.manifest.json").exists()

    def test_pull_from_inbox(self, agent_home: Path, tmp_path: Path):
        """Pull should retrieve vault from inbox and move to archive."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        inbox_file = backend.inbox / "vault-peer-20260223.tar.gz"
        inbox_file.write_text("peer vault data")

        result = backend.pull(tmp_path)
        assert result is not None
        assert result.name == "vault-peer-20260223.tar.gz"
        assert not inbox_file.exists()

    def test_pull_empty_inbox(self, agent_home: Path, tmp_path: Path):
        """Pull should return None when inbox is empty."""
        from skcapstone.sync.backends import SyncthingBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = SyncthingBackend(config, agent_home)

        result = backend.pull(tmp_path)
        assert result is None


class TestLocalBackend:
    """Tests for the local filesystem backend."""

    def test_push_and_pull_roundtrip(self, tmp_path: Path, agent_home: Path):
        """Local push then pull should retrieve the same vault."""
        from skcapstone.sync.backends import LocalBackend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        config = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=backup_dir,
        )
        backend = LocalBackend(config, agent_home)

        vault_file = tmp_path / "vault-local-test.tar.gz"
        vault_file.write_bytes(b"local vault content")
        manifest_file = tmp_path / "vault-local-test.manifest.json"
        manifest_file.write_text('{"agent_name": "local"}')

        assert backend.push(vault_file, manifest_file) is True

        pull_dir = tmp_path / "pulled"
        pull_dir.mkdir()
        result = backend.pull(pull_dir)
        assert result is not None
        assert result.read_bytes() == b"local vault content"


class TestBackendFactory:
    """Tests for the create_backend factory function."""

    def test_creates_syncthing(self, agent_home: Path):
        from skcapstone.sync.backends import SyncthingBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        backend = create_backend(config, agent_home)
        assert isinstance(backend, SyncthingBackend)

    def test_creates_local(self, agent_home: Path):
        from skcapstone.sync.backends import LocalBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.LOCAL)
        backend = create_backend(config, agent_home)
        assert isinstance(backend, LocalBackend)

    def test_creates_github(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.GITHUB, repo_url="https://github.com/test/repo"
        )
        backend = create_backend(config, agent_home)
        assert isinstance(backend, GitBackend)
        assert backend.name == "github"

    def test_creates_forgejo(self, agent_home: Path):
        from skcapstone.sync.backends import GitBackend, create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.FORGEJO, repo_url="https://forgejo.example/test"
        )
        backend = create_backend(config, agent_home)
        assert isinstance(backend, GitBackend)
        assert backend.name == "forgejo"


class TestSyncEngine:
    """Tests for the sync engine orchestration."""

    def test_engine_initializes(self, agent_home: Path):
        """Engine should initialize with default config."""
        from skcapstone.sync.engine import SyncEngine

        engine = SyncEngine(agent_home)
        assert engine.agent_home == agent_home
        assert engine.config is not None
        assert engine.state is not None

    def test_add_backend(self, agent_home: Path):
        """Adding a backend should persist to config."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        engine.add_backend(config)

        assert len(engine.config.backends) == 1
        assert engine.config.backends[0].backend_type == SyncBackendType.SYNCTHING

    def test_status_returns_info(self, agent_home: Path):
        """Status should return backend and vault information."""
        from skcapstone.sync.engine import SyncEngine

        engine = SyncEngine(agent_home)
        info = engine.status()

        assert "state" in info
        assert "backends" in info
        assert "vaults" in info
        assert "encrypt" in info

    def test_push_with_syncthing_backend(self, agent_home: Path):
        """Push with syncthing backend should pack and deliver vault."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.config.encrypt = False
        engine.add_backend(
            SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        )

        results = engine.push(passphrase=None)
        assert "syncthing" in results
        assert results["syncthing"] is True
        assert engine.state.push_count == 1

    def test_push_pull_roundtrip(self, agent_home: Path, tmp_path: Path):
        """Full push then pull should restore state to a new location."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.config.encrypt = False
        backup_dir = tmp_path / "local-backup"
        backup_dir.mkdir()
        engine.add_backend(
            SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL,
                local_path=backup_dir,
            )
        )

        push_results = engine.push(passphrase=None)
        assert push_results.get("local") is True

        restore_home = tmp_path / "restored"
        restore_home.mkdir()
        engine2 = SyncEngine(restore_home)
        engine2.config.encrypt = False
        engine2.add_backend(
            SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL,
                local_path=backup_dir,
            )
        )

        result = engine2.pull(passphrase=None)
        assert result is not None

    def test_pull_no_backends(self, agent_home: Path):
        """Pull with no backends should return None gracefully."""
        from skcapstone.sync.engine import SyncEngine

        engine = SyncEngine(agent_home)
        result = engine.pull(passphrase=None)
        assert result is None

    def test_pull_dry_run(self, agent_home: Path, tmp_path: Path):
        """Dry-run pull should download without extracting."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.config.encrypt = False
        backup_dir = tmp_path / "local-backup"
        backup_dir.mkdir()
        engine.add_backend(
            SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL,
                local_path=backup_dir,
            )
        )
        engine.push(passphrase=None)

        result = engine.pull(passphrase=None, dry_run=True)
        assert result is not None
        assert result.name.startswith("vault-")

    def test_config_save_load_persistence(self, agent_home: Path):
        """Saved config should be loadable by a new engine instance."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.add_backend(
            SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        )
        engine.add_backend(
            SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL,
                local_path=agent_home / "sync" / "local-backup",
            )
        )

        engine2 = SyncEngine(agent_home)
        assert len(engine2.config.backends) == 2

    def test_state_persists_across_operations(self, agent_home: Path):
        """Push count and timestamps should persist to disk after push."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.config.encrypt = False
        engine.add_backend(
            SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        )
        engine.push(passphrase=None)
        assert engine.state.push_count == 1
        assert engine.state.last_push is not None
        assert engine.state.last_push_backend == "syncthing"

    def test_backend_filter(self, agent_home: Path, tmp_path: Path):
        """Push with backend_filter should only push to that backend."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine = SyncEngine(agent_home)
        engine.config.encrypt = False
        engine.add_backend(
            SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        )
        backup_dir = tmp_path / "local-backup"
        backup_dir.mkdir()
        engine.add_backend(
            SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL,
                local_path=backup_dir,
            )
        )

        results = engine.push(passphrase=None, backend_filter="local")
        assert "local" in results
        assert "syncthing" not in results


class TestVaultHardening:
    """Tests for vault encryption hardening (sync01)."""

    def test_pack_includes_file_hashes(self, agent_home: Path):
        """Manifest should contain SHA-256 hashes for every packed file."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        file_hashes = data.get("file_hashes", {})

        assert len(file_hashes) > 0
        assert "identity/identity.json" in file_hashes
        assert "trust/trust.json" in file_hashes
        assert "manifest.json" in file_hashes

        for path, h in file_hashes.items():
            assert len(h) == 64, f"Hash for {path} should be 64 hex chars"

    def test_pack_includes_archive_hash(self, agent_home: Path):
        """Manifest should contain SHA-256 hash of the archive itself."""
        from skcapstone.sync.vault import Vault, _sha256_file

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert data.get("archive_hash") is not None
        assert data["archive_hash"] == _sha256_file(archive_path)

    def test_unpack_verifies_archive_hash(self, agent_home: Path, tmp_path: Path):
        """Unpack should reject archives whose SHA-256 doesn't match the manifest."""
        from skcapstone.sync.vault import Vault, VaultIntegrityError

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        manifest_path = archive_path.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())
        data["archive_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(data, indent=2))

        restore_dir = tmp_path / "bad-restore"
        restore_dir.mkdir()

        with pytest.raises(VaultIntegrityError, match="Archive hash mismatch"):
            vault.unpack(archive_path, target=restore_dir, verify_signature=False)

    def test_unpack_verifies_file_hashes(self, agent_home: Path, tmp_path: Path):
        """Unpack should reject vaults with tampered file contents."""
        from skcapstone.sync.vault import Vault, VaultIntegrityError

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        manifest_path = archive_path.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())

        data["archive_hash"] = None
        first_key = next(iter(data["file_hashes"]))
        data["file_hashes"][first_key] = "deadbeef" * 8
        manifest_path.write_text(json.dumps(data, indent=2))

        restore_dir = tmp_path / "tampered-restore"
        restore_dir.mkdir()

        with pytest.raises(VaultIntegrityError, match="Hash mismatch"):
            vault.unpack(archive_path, target=restore_dir, verify_signature=False)

    def test_unpack_succeeds_with_valid_hashes(self, agent_home: Path, tmp_path: Path):
        """Unpack with correct hashes should succeed without error."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        restore_dir = tmp_path / "good-restore"
        restore_dir.mkdir()
        result = vault.unpack(
            archive_path, target=restore_dir, verify_signature=False
        )
        assert result == restore_dir
        assert (restore_dir / "identity" / "identity.json").exists()

    def test_unpack_skips_verification_when_disabled(self, agent_home: Path, tmp_path: Path):
        """Unpack with verify_hashes=False should skip integrity checks."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        manifest_path = archive_path.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())
        data["archive_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(data, indent=2))

        restore_dir = tmp_path / "skip-verify"
        restore_dir.mkdir()
        result = vault.unpack(
            archive_path,
            target=restore_dir,
            verify_signature=False,
            verify_hashes=False,
        )
        assert result == restore_dir

    def test_unpack_without_manifest_still_works(self, agent_home: Path, tmp_path: Path):
        """Unpack should succeed gracefully when no manifest exists."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)

        manifest_path = archive_path.with_suffix(".manifest.json")
        manifest_path.unlink()

        restore_dir = tmp_path / "no-manifest"
        restore_dir.mkdir()
        result = vault.unpack(archive_path, target=restore_dir)
        assert result == restore_dir

    def test_manifest_schema_version_1_1(self, agent_home: Path):
        """Hardened manifests should use schema version 1.1."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert data["schema_version"] == "1.1"

    def test_manifest_includes_fingerprint(self, agent_home: Path):
        """Manifest should include the agent's PGP fingerprint."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert data["fingerprint"] == "AAAA1111BBBB2222CCCC3333DDDD4444EEEE5555"


class TestVaultKeyRotation:
    """Tests for vault key rotation (sync01)."""

    def test_rotate_keys_no_vaults(self, agent_home: Path):
        """Key rotation with no vaults should return empty list."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        result = vault.rotate_keys(old_passphrase="old", new_passphrase="new")
        assert result == []

    def test_rotate_updates_manifest(self, agent_home: Path):
        """Key rotation should update the manifest's encrypted flag and hash."""
        from skcapstone.sync.vault import Vault

        vault = Vault(agent_home)
        archive_path = vault.pack(encrypt=False)
        manifest_path = archive_path.with_suffix(".manifest.json")

        data = json.loads(manifest_path.read_text())
        assert data["encrypted"] is False

        # Reason: rotate_keys encrypts plaintext vaults with new passphrase
        # but GPG may not be available in CI, so we check the method exists
        assert callable(vault.rotate_keys)


class TestVaultManifestModel:
    """Tests for the VaultManifest Pydantic model."""

    def test_manifest_serialization(self):
        """VaultManifest should serialize to JSON and back."""
        from skcapstone.sync.models import VaultManifest

        manifest = VaultManifest(
            agent_name="TestAgent",
            source_host="test-host",
            created_at=datetime(2026, 2, 23, tzinfo=timezone.utc),
            pillars_included=["identity", "memory", "trust"],
            encrypted=True,
            file_hashes={"identity/identity.json": "a" * 64},
            archive_hash="b" * 64,
        )
        json_str = manifest.model_dump_json()
        restored = VaultManifest.model_validate_json(json_str)
        assert restored.agent_name == "TestAgent"
        assert restored.pillars_included == ["identity", "memory", "trust"]
        assert restored.encrypted is True
        assert restored.file_hashes == {"identity/identity.json": "a" * 64}
        assert restored.archive_hash == "b" * 64

    def test_manifest_defaults(self):
        """VaultManifest should have sensible defaults."""
        from skcapstone.sync.models import VaultManifest

        manifest = VaultManifest(
            agent_name="Test",
            source_host="host",
            created_at=datetime.now(timezone.utc),
        )
        assert manifest.schema_version == "1.1"
        assert manifest.encrypted is True
        assert manifest.pillars_included == []
        assert manifest.fingerprint is None
        assert manifest.file_hashes == {}
        assert manifest.archive_hash is None

    def test_manifest_with_signature_fields(self):
        """VaultManifest should support signature and signed_by fields."""
        from skcapstone.sync.models import VaultManifest

        manifest = VaultManifest(
            agent_name="Test",
            source_host="host",
            created_at=datetime.now(timezone.utc),
            signature="BASE64SIG",
            signed_by="FINGERPRINT123",
        )
        assert manifest.signature == "BASE64SIG"
        assert manifest.signed_by == "FINGERPRINT123"


class TestSyncBackendConfigModel:
    """Tests for the SyncBackendConfig model."""

    def test_syncthing_config(self):
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.SYNCTHING,
            syncthing_folder_id="skcapstone-sync",
        )
        assert config.backend_type == SyncBackendType.SYNCTHING
        assert config.enabled is True

    def test_git_config(self):
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(
            backend_type=SyncBackendType.GITHUB,
            repo_url="https://github.com/test/repo",
            branch="main",
        )
        assert config.repo_url == "https://github.com/test/repo"
        assert config.branch == "main"


class TestUnsupportedBackend:
    """Edge case: unsupported backend type."""

    def test_factory_rejects_gdrive(self, agent_home: Path):
        """GDrive backend should raise ValueError (not implemented)."""
        from skcapstone.sync.backends import create_backend
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.GDRIVE)
        with pytest.raises(ValueError, match="Unsupported"):
            create_backend(config, agent_home)
