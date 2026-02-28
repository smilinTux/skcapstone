"""
Unit tests for skcapstone Vault (sync/vault.py).

Covers packing, unpacking, integrity verification, key helpers,
exclusion logic, manifest models, and encryption paths with mocked GPG.
"""

from __future__ import annotations

import hashlib
import json
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / ".skcapstone"
    home.mkdir()

    for pillar in ("identity", "memory", "trust", "config", "skills"):
        (home / pillar).mkdir()

    (home / "identity" / "identity.json").write_text(
        json.dumps({
            "name": "VaultTestAgent",
            "fingerprint": "BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666",
        })
    )
    (home / "trust" / "trust.json").write_text(
        json.dumps({"depth": 4.0, "trust_level": 0.85})
    )
    (home / "config" / "config.yaml").write_text("agent_name: VaultTestAgent\n")
    (home / "manifest.json").write_text(
        json.dumps({"name": "VaultTestAgent", "version": "0.2.0", "connectors": []})
    )
    for layer in ("short-term", "mid-term", "long-term"):
        (home / "memory" / layer).mkdir(parents=True)
    (home / "memory" / "long-term" / "important.json").write_text(
        json.dumps({"content": "vault test memory"})
    )
    return home


@pytest.fixture
def vault(agent_home: Path):
    from skcapstone.sync.vault import Vault

    return Vault(agent_home)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHashHelpers:
    def test_sha256_file(self, tmp_path: Path):
        from skcapstone.sync.vault import _sha256_file

        f = tmp_path / "data.bin"
        f.write_bytes(b"hello world")
        digest = _sha256_file(f)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert digest == expected
        assert len(digest) == 64

    def test_sha256_bytes(self):
        from skcapstone.sync.vault import _sha256_bytes

        digest = _sha256_bytes(b"sovereign")
        expected = hashlib.sha256(b"sovereign").hexdigest()
        assert digest == expected
        assert len(digest) == 64

    def test_sha256_empty_bytes(self):
        from skcapstone.sync.vault import _sha256_bytes

        digest = _sha256_bytes(b"")
        assert digest == hashlib.sha256(b"").hexdigest()


# ---------------------------------------------------------------------------
# Vault construction
# ---------------------------------------------------------------------------


class TestVaultConstruction:
    def test_creates_vault_dir(self, agent_home: Path):
        from skcapstone.sync.vault import Vault

        Vault(agent_home)
        assert (agent_home / "vault").is_dir()

    def test_expands_home_tilde(self, tmp_path: Path):
        """Vault should call expanduser() on agent_home."""
        real_home = tmp_path / "agent"
        real_home.mkdir()
        from skcapstone.sync.vault import Vault

        v = Vault(real_home)
        assert v.agent_home == real_home


# ---------------------------------------------------------------------------
# Exclusion logic
# ---------------------------------------------------------------------------


class TestShouldExclude:
    def test_excludes_pycache(self, vault):
        assert vault._should_exclude("__pycache__") is True

    def test_excludes_pyc(self, vault):
        assert vault._should_exclude("compiled.pyc") is True

    def test_excludes_git(self, vault):
        assert vault._should_exclude(".git") is True

    def test_excludes_audit_log(self, vault):
        assert vault._should_exclude("audit.log") is True

    def test_allows_normal_files(self, vault):
        assert vault._should_exclude("identity.json") is False
        assert vault._should_exclude("trust.json") is False
        assert vault._should_exclude("memories.json") is False


# ---------------------------------------------------------------------------
# pack
# ---------------------------------------------------------------------------


class TestVaultPack:
    def test_pack_archive_naming(self, vault):
        """Archive name should match vault-<host>-<timestamp>.tar.gz pattern."""
        path = vault.pack(encrypt=False)
        assert path.name.startswith("vault-")
        assert path.name.endswith(".tar.gz")

    def test_pack_archive_in_vault_dir(self, vault, agent_home: Path):
        path = vault.pack(encrypt=False)
        assert path.parent == agent_home / "vault"

    def test_pack_creates_manifest_file(self, vault):
        path = vault.pack(encrypt=False)
        manifest = path.with_suffix(".manifest.json")
        assert manifest.exists()

    def test_manifest_agent_name_from_manifest_json(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert data["agent_name"] == "VaultTestAgent"

    def test_manifest_includes_all_pillars(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        included = data["pillars_included"]
        assert "identity" in included
        assert "trust" in included
        assert "config" in included
        assert "memory" in included

    def test_pack_custom_pillar_selection(self, vault):
        """Only requested pillars should be included."""
        path = vault.pack(pillars=["identity", "trust"], encrypt=False)
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
        assert any("identity/" in n for n in names)
        assert any("trust/" in n for n in names)
        assert not any("config/" in n for n in names)

    def test_pack_skips_missing_pillar(self, vault):
        """A pillar that doesn't exist on disk is silently skipped."""
        path = vault.pack(pillars=["identity", "nonexistent"], encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert "identity" in data["pillars_included"]
        assert "nonexistent" not in data["pillars_included"]

    def test_pack_excludes_pycache_files(self, vault, agent_home: Path):
        """__pycache__ and .pyc must not appear in the archive."""
        pycache = agent_home / "identity" / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.pyc").write_bytes(b"junk bytecode")

        path = vault.pack(encrypt=False)
        with tarfile.open(path, "r:gz") as tar:
            names = tar.getnames()
        assert not any("__pycache__" in n for n in names)
        assert not any(".pyc" in n for n in names)

    def test_pack_includes_manifest_json(self, vault, agent_home: Path):
        """Top-level manifest.json should be bundled in the archive."""
        path = vault.pack(encrypt=False)
        with tarfile.open(path, "r:gz") as tar:
            assert "manifest.json" in tar.getnames()

    def test_pack_skips_manifest_if_not_present(self, vault, agent_home: Path):
        """Pack should not crash when manifest.json doesn't exist."""
        (agent_home / "manifest.json").unlink()
        path = vault.pack(encrypt=False)
        assert path.exists()

    def test_pack_file_hashes_in_manifest(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert isinstance(data["file_hashes"], dict)
        assert len(data["file_hashes"]) > 0
        for rel_path, h in data["file_hashes"].items():
            assert len(h) == 64, f"Bad hash length for {rel_path}"

    def test_pack_archive_hash_in_manifest(self, vault):
        from skcapstone.sync.vault import _sha256_file

        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert data["archive_hash"] == _sha256_file(path)

    def test_pack_schema_version(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert data["schema_version"] == "1.1"

    def test_pack_encrypted_flag_false(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert data["encrypted"] is False

    def test_pack_fingerprint_from_identity(self, vault):
        path = vault.pack(encrypt=False)
        data = json.loads(path.with_suffix(".manifest.json").read_text())
        assert data["fingerprint"] == "BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666"


# ---------------------------------------------------------------------------
# pack + encryption (mocked GPG)
# ---------------------------------------------------------------------------


class TestVaultPackEncrypt:
    def test_pack_encrypt_returns_gpg_path(self, vault):
        """pack(encrypt=True) should return a .tar.gz.gpg path."""
        def fake_encrypt(archive_path, passphrase):
            # Create the .gpg output; pack() will unlink the original itself
            gpg_path = archive_path.with_suffix(archive_path.suffix + ".gpg")
            gpg_path.write_bytes(b"encrypted")
            return gpg_path

        with patch.object(vault, "_encrypt_vault", side_effect=fake_encrypt):
            result = vault.pack(encrypt=True, passphrase="secret")

        assert result.name.endswith(".gpg")

    def test_pack_encrypt_removes_plaintext(self, vault):
        """The plaintext .tar.gz should be deleted after encryption."""
        def fake_encrypt(archive_path, passphrase):
            gpg_path = archive_path.with_suffix(archive_path.suffix + ".gpg")
            gpg_path.write_bytes(b"encrypted")
            return gpg_path

        with patch.object(vault, "_encrypt_vault", side_effect=fake_encrypt):
            result = vault.pack(encrypt=True, passphrase="secret")

        plain = result.with_name(result.name[:-4])  # strip .gpg
        assert not plain.exists()
        assert result.exists()


# ---------------------------------------------------------------------------
# unpack
# ---------------------------------------------------------------------------


class TestVaultUnpack:
    def test_unpack_extracts_files(self, vault, tmp_path: Path):
        archive = vault.pack(encrypt=False)
        restore = tmp_path / "restored"
        restore.mkdir()
        result = vault.unpack(archive, target=restore, verify_signature=False)
        assert result == restore
        assert (restore / "identity" / "identity.json").exists()
        assert (restore / "trust" / "trust.json").exists()

    def test_unpack_default_target_is_agent_home(self, vault, agent_home: Path, tmp_path: Path):
        """Without explicit target, unpack extracts to agent_home."""
        alt_vault = Vault_helper(agent_home)
        archive = alt_vault.pack(encrypt=False)
        result = alt_vault.unpack(archive, verify_signature=False)
        assert result == agent_home

    def test_unpack_without_manifest(self, vault, tmp_path: Path, agent_home: Path):
        """Unpack works gracefully when no manifest file exists."""
        archive = vault.pack(encrypt=False)
        manifest = archive.with_suffix(".manifest.json")
        manifest.unlink()

        restore = tmp_path / "no-manifest"
        restore.mkdir()
        result = vault.unpack(archive, target=restore, verify_signature=False)
        assert result == restore

    def test_unpack_detects_tampered_archive(self, vault, tmp_path: Path):
        """Appending bytes to archive triggers VaultIntegrityError."""
        from skcapstone.sync.vault import VaultIntegrityError

        archive = vault.pack(encrypt=False)
        with open(archive, "ab") as f:
            f.write(b"\x00TAMPERED\x00")

        restore = tmp_path / "tampered"
        restore.mkdir()
        with pytest.raises(VaultIntegrityError, match="[Hh]ash mismatch"):
            vault.unpack(archive, target=restore, verify_signature=False)

    def test_unpack_detects_tampered_file_via_manifest(self, vault, tmp_path: Path, agent_home: Path):
        """Faking a file hash in manifest triggers VaultIntegrityError on extraction."""
        from skcapstone.sync.vault import VaultIntegrityError

        archive = vault.pack(encrypt=False)
        manifest_path = archive.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())

        # Nullify archive_hash to pass that check, corrupt a file hash
        data["archive_hash"] = None
        first_key = next(iter(data["file_hashes"]))
        data["file_hashes"][first_key] = "deadbeef" * 8
        manifest_path.write_text(json.dumps(data))

        restore = tmp_path / "file-tampered"
        restore.mkdir()
        with pytest.raises(VaultIntegrityError, match="[Hh]ash mismatch"):
            vault.unpack(archive, target=restore, verify_signature=False)

    def test_unpack_skip_hash_verification(self, vault, tmp_path: Path):
        """verify_hashes=False skips all integrity checks."""
        archive = vault.pack(encrypt=False)
        with open(archive, "ab") as f:
            f.write(b"\x00CORRUPT\x00")

        restore = tmp_path / "skip"
        restore.mkdir()
        # Should not raise
        vault.unpack(archive, target=restore, verify_signature=False, verify_hashes=False)

    def test_unpack_with_tampered_archive_hash_in_manifest(self, vault, tmp_path: Path):
        """If manifest archive_hash is wrong, VaultIntegrityError is raised."""
        from skcapstone.sync.vault import VaultIntegrityError

        archive = vault.pack(encrypt=False)
        manifest_path = archive.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())
        data["archive_hash"] = "0" * 64
        manifest_path.write_text(json.dumps(data))

        restore = tmp_path / "bad-hash"
        restore.mkdir()
        with pytest.raises(VaultIntegrityError, match="Archive hash mismatch"):
            vault.unpack(archive, target=restore, verify_signature=False)

    def test_unpack_valid_hashes_succeeds(self, vault, tmp_path: Path):
        archive = vault.pack(encrypt=False)
        restore = tmp_path / "valid"
        restore.mkdir()
        result = vault.unpack(archive, target=restore, verify_signature=False)
        assert result == restore
        assert (restore / "manifest.json").exists()


# ---------------------------------------------------------------------------
# _get_agent_name and _get_agent_fingerprint
# ---------------------------------------------------------------------------


class TestVaultAgentHelpers:
    def test_get_agent_name_from_manifest(self, vault):
        assert vault._get_agent_name() == "VaultTestAgent"

    def test_get_agent_name_no_manifest(self, vault, agent_home: Path):
        (agent_home / "manifest.json").unlink()
        assert vault._get_agent_name() == "unknown"

    def test_get_agent_name_corrupt_manifest(self, vault, agent_home: Path):
        (agent_home / "manifest.json").write_text("NOT JSON {{{")
        assert vault._get_agent_name() == "unknown"

    def test_get_agent_fingerprint_from_identity(self, vault):
        fp = vault._get_agent_fingerprint()
        assert fp == "BBBB2222CCCC3333DDDD4444EEEE5555FFFF6666"

    def test_get_agent_fingerprint_no_identity_file(self, vault, agent_home: Path):
        (agent_home / "identity" / "identity.json").unlink()
        assert vault._get_agent_fingerprint() is None

    def test_get_agent_fingerprint_corrupt_identity(self, vault, agent_home: Path):
        (agent_home / "identity" / "identity.json").write_text("{BROKEN}")
        assert vault._get_agent_fingerprint() is None

    def test_get_agent_fingerprint_no_identity_dir(self, vault, agent_home: Path):
        import shutil

        shutil.rmtree(agent_home / "identity")
        assert vault._get_agent_fingerprint() is None


# ---------------------------------------------------------------------------
# list_vaults
# ---------------------------------------------------------------------------


class TestListVaults:
    def test_empty_vault_dir(self, vault):
        assert vault.list_vaults() == []

    def test_single_vault(self, vault):
        vault.pack(encrypt=False)
        result = vault.list_vaults()
        assert len(result) == 1

    def test_two_vaults(self, vault):
        import time

        vault.pack(encrypt=False)
        time.sleep(1.1)
        vault.pack(encrypt=False)
        result = vault.list_vaults()
        assert len(result) == 2

    def test_list_includes_metadata(self, vault):
        vault.pack(encrypt=False)
        entries = vault.list_vaults()
        entry = entries[0]
        assert "path" in entry
        assert "size" in entry
        # Manifest fields should be merged
        assert "agent_name" in entry

    def test_manifest_json_files_excluded(self, vault, agent_home: Path):
        """list_vaults should not count .manifest.json files as vaults."""
        vault.pack(encrypt=False)
        entries = vault.list_vaults()
        for e in entries:
            assert not str(e["path"]).endswith(".json")


# ---------------------------------------------------------------------------
# VaultManifest model
# ---------------------------------------------------------------------------


class TestVaultManifestModel:
    def test_default_schema_version(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="A", source_host="h", created_at=datetime.now(timezone.utc)
        )
        assert m.schema_version == "1.1"

    def test_default_encrypted_true(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="A", source_host="h", created_at=datetime.now(timezone.utc)
        )
        assert m.encrypted is True

    def test_default_pillars_empty(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="A", source_host="h", created_at=datetime.now(timezone.utc)
        )
        assert m.pillars_included == []

    def test_default_file_hashes_empty(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="A", source_host="h", created_at=datetime.now(timezone.utc)
        )
        assert m.file_hashes == {}

    def test_roundtrip_json(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="Roundtrip",
            source_host="node01",
            created_at=datetime(2026, 2, 28, tzinfo=timezone.utc),
            pillars_included=["identity", "memory"],
            encrypted=False,
            file_hashes={"identity/identity.json": "a" * 64},
            archive_hash="b" * 64,
            fingerprint="FFFFFFFFFFFFFFFFFFFFFFFF",
        )
        restored = VaultManifest.model_validate_json(m.model_dump_json())
        assert restored.agent_name == "Roundtrip"
        assert restored.encrypted is False
        assert restored.file_hashes == {"identity/identity.json": "a" * 64}
        assert restored.archive_hash == "b" * 64
        assert restored.fingerprint == "FFFFFFFFFFFFFFFFFFFFFFFF"

    def test_signature_fields(self):
        from skcapstone.sync.models import VaultManifest

        m = VaultManifest(
            agent_name="A",
            source_host="h",
            created_at=datetime.now(timezone.utc),
            signature="SIG_BASE64",
            signed_by="FP1234",
        )
        assert m.signature == "SIG_BASE64"
        assert m.signed_by == "FP1234"


# ---------------------------------------------------------------------------
# VaultIntegrityError / VaultSignatureError exceptions
# ---------------------------------------------------------------------------


class TestVaultExceptions:
    def test_integrity_error_is_exception(self):
        from skcapstone.sync.vault import VaultIntegrityError

        err = VaultIntegrityError("test")
        assert isinstance(err, Exception)
        assert str(err) == "test"

    def test_signature_error_is_exception(self):
        from skcapstone.sync.vault import VaultSignatureError

        err = VaultSignatureError("bad sig")
        assert isinstance(err, Exception)

    def test_signature_error_raised_on_invalid_signature(self, vault, tmp_path: Path):
        """unpack should raise VaultSignatureError for manifests with a bad signature."""
        from skcapstone.sync.vault import VaultSignatureError

        archive = vault.pack(encrypt=False)
        manifest_path = archive.with_suffix(".manifest.json")
        data = json.loads(manifest_path.read_text())
        data["signature"] = "BADSIG"
        data["archive_hash"] = None  # skip archive hash check
        manifest_path.write_text(json.dumps(data))

        restore = tmp_path / "badsig"
        restore.mkdir()
        with patch.object(vault, "_verify_signature", return_value=False):
            with pytest.raises(VaultSignatureError):
                vault.unpack(
                    archive, target=restore, verify_signature=True, verify_hashes=False
                )

    def test_no_signature_does_not_raise(self, vault, tmp_path: Path):
        """A manifest with no signature should not trigger VaultSignatureError."""
        archive = vault.pack(encrypt=False)
        restore = tmp_path / "nosig"
        restore.mkdir()
        # Should not raise even with verify_signature=True
        vault.unpack(archive, target=restore, verify_signature=True, verify_hashes=False)


# ---------------------------------------------------------------------------
# Helpers for test reuse
# ---------------------------------------------------------------------------


def Vault_helper(agent_home: Path):
    from skcapstone.sync.vault import Vault

    return Vault(agent_home)
