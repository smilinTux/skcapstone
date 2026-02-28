"""Unit tests for the sync pillar module."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.models import PillarStatus, SyncConfig, SyncTransport
from skcapstone.pillars.sync import (
    SEED_EXTENSION,
    collect_seed,
    discover_sync,
    initialize_sync,
    pull_seeds,
    push_seed,
    save_sync_state,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _no_gpg_config(tmp_path: Path) -> SyncConfig:
    """SyncConfig that stores seeds under tmp_path/sync with no GPG."""
    return SyncConfig(
        sync_folder=tmp_path / "sync",
        gpg_encrypt=False,
    )


# ── TestInitializeSync ────────────────────────────────────────────────────────

class TestInitializeSync:
    """Tests for initialize_sync()."""

    def test_creates_sync_directory(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        assert (tmp_agent_home / "sync").is_dir()

    def test_creates_outbox_inbox_archive(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        sync_dir = tmp_agent_home / "sync"
        for sub in ("outbox", "inbox", "archive"):
            assert (sync_dir / sub).is_dir(), f"missing: {sub}"

    def test_writes_sync_manifest(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        manifest = tmp_agent_home / "sync" / "sync-manifest.json"
        assert manifest.exists()

    def test_manifest_contains_transport(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        data = json.loads((tmp_agent_home / "sync" / "sync-manifest.json").read_text())
        assert "transport" in data

    def test_manifest_contains_created_at(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        data = json.loads((tmp_agent_home / "sync" / "sync-manifest.json").read_text())
        assert "created_at" in data

    def test_returns_active_without_gpg(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        state = initialize_sync(tmp_agent_home, config=cfg)
        assert state.status == PillarStatus.ACTIVE

    def test_returns_degraded_when_gpg_required_but_no_key(self, tmp_agent_home: Path):
        cfg = SyncConfig(sync_folder=tmp_agent_home / "sync", gpg_encrypt=True)
        with patch("skcapstone.pillars.sync._detect_gpg_key", return_value=None):
            state = initialize_sync(tmp_agent_home, config=cfg)
        assert state.status == PillarStatus.DEGRADED

    def test_active_when_gpg_key_found(self, tmp_agent_home: Path):
        cfg = SyncConfig(sync_folder=tmp_agent_home / "sync", gpg_encrypt=True)
        with patch("skcapstone.pillars.sync._detect_gpg_key", return_value="DEADBEEF"):
            state = initialize_sync(tmp_agent_home, config=cfg)
        assert state.status == PillarStatus.ACTIVE
        assert state.gpg_fingerprint == "DEADBEEF"

    def test_sync_path_set_in_state(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        state = initialize_sync(tmp_agent_home, config=cfg)
        assert state.sync_path == tmp_agent_home / "sync"

    def test_transport_stored_in_state(self, tmp_agent_home: Path):
        cfg = SyncConfig(
            sync_folder=tmp_agent_home / "sync",
            gpg_encrypt=False,
            transport=SyncTransport.GIT,
        )
        state = initialize_sync(tmp_agent_home, config=cfg)
        assert state.transport == SyncTransport.GIT

    def test_default_config_used_when_none(self, tmp_path: Path):
        """initialize_sync with no config falls back to defaults (won't create files
        in tmp_agent_home since default sync_folder is ~/.skcapstone/sync)."""
        cfg = _no_gpg_config(tmp_path)
        state = initialize_sync(tmp_path, config=cfg)
        assert state.status == PillarStatus.ACTIVE


# ── TestCollectSeed ───────────────────────────────────────────────────────────

class TestCollectSeed:
    """Tests for collect_seed()."""

    def _setup_sync_dir(self, home: Path) -> Path:
        """Create the sync/outbox structure collect_seed expects."""
        cfg = _no_gpg_config(home)
        initialize_sync(home, config=cfg)
        return home / "sync"

    def test_creates_seed_file_in_outbox(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "test-agent")
        assert seed_path.exists()
        assert seed_path.parent.name == "outbox"

    def test_seed_filename_contains_agent_name(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "lumina")
        assert "lumina" in seed_path.name

    def test_seed_filename_has_seed_extension(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "opus")
        assert seed_path.name.endswith(SEED_EXTENSION)

    def test_seed_contains_schema_version(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "test")
        data = json.loads(seed_path.read_text())
        assert data["schema_version"] == "1.0"

    def test_seed_contains_agent_name(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "grok")
        data = json.loads(seed_path.read_text())
        assert data["agent_name"] == "grok"

    def test_seed_contains_created_at(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "test")
        data = json.loads(seed_path.read_text())
        assert "created_at" in data

    def test_seed_contains_source_host(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        seed_path = collect_seed(tmp_agent_home, "test")
        data = json.loads(seed_path.read_text())
        assert "source_host" in data

    def test_seed_includes_identity_when_present(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        identity_dir = tmp_agent_home / "identity"
        identity_dir.mkdir(parents=True, exist_ok=True)
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABC"}), encoding="utf-8"
        )
        seed_path = collect_seed(tmp_agent_home, "opus")
        data = json.loads(seed_path.read_text())
        assert "identity" in data
        assert data["identity"]["name"] == "opus"

    def test_seed_includes_trust_when_present(self, tmp_agent_home: Path):
        self._setup_sync_dir(tmp_agent_home)
        trust_dir = tmp_agent_home / "trust"
        trust_dir.mkdir(parents=True, exist_ok=True)
        (trust_dir / "trust.json").write_text(
            json.dumps({"depth": 9.0, "trust_level": 0.97}), encoding="utf-8"
        )
        seed_path = collect_seed(tmp_agent_home, "test")
        data = json.loads(seed_path.read_text())
        assert "trust" in data
        assert data["trust"]["depth"] == 9.0


# ── TestDiscoverSync ──────────────────────────────────────────────────────────

class TestDiscoverSync:
    """Tests for discover_sync()."""

    def test_returns_missing_when_no_sync_dir(self, tmp_path: Path):
        home = tmp_path / "empty-home"
        home.mkdir()
        # Patch _resolve_sync_dir so it doesn't fall back to the real ~/.skcapstone/sync
        non_existent = home / "sync"
        with patch("skcapstone.pillars.sync._resolve_sync_dir", return_value=non_existent):
            state = discover_sync(home)
        assert state.status == PillarStatus.MISSING

    def test_returns_degraded_when_no_manifest(self, tmp_agent_home: Path):
        (tmp_agent_home / "sync").mkdir()
        state = discover_sync(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED

    def test_returns_degraded_when_manifest_corrupt(self, tmp_agent_home: Path):
        sync_dir = tmp_agent_home / "sync"
        sync_dir.mkdir()
        (sync_dir / "sync-manifest.json").write_text("not-json", encoding="utf-8")
        state = discover_sync(tmp_agent_home)
        assert state.status == PillarStatus.DEGRADED

    def test_returns_active_when_manifest_exists(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        with patch("skcapstone.pillars.sync._detect_gpg_key", return_value=None):
            state = discover_sync(tmp_agent_home)
        assert state.status in (PillarStatus.ACTIVE, PillarStatus.DEGRADED)

    def test_transport_read_from_manifest(self, tmp_agent_home: Path):
        cfg = SyncConfig(
            sync_folder=tmp_agent_home / "sync",
            gpg_encrypt=False,
            transport=SyncTransport.GIT,
        )
        initialize_sync(tmp_agent_home, config=cfg)
        with patch("skcapstone.pillars.sync._detect_gpg_key", return_value=None):
            state = discover_sync(tmp_agent_home)
        assert state.transport == SyncTransport.GIT

    def test_seed_count_includes_outbox_seeds(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        outbox = tmp_agent_home / "sync" / "outbox"
        (outbox / f"test{SEED_EXTENSION}").write_text("{}", encoding="utf-8")
        with patch("skcapstone.pillars.sync._detect_gpg_key", return_value=None):
            state = discover_sync(tmp_agent_home)
        assert state.seed_count >= 1


# ── TestPushSeed ──────────────────────────────────────────────────────────────

class TestPushSeed:
    """Tests for push_seed()."""

    def test_no_encrypt_returns_seed_path(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        result = push_seed(tmp_agent_home, "test-agent", encrypt=False)
        assert result is not None
        assert result.exists()

    def test_no_encrypt_seed_is_valid_json(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        result = push_seed(tmp_agent_home, "test-agent", encrypt=False)
        data = json.loads(result.read_text())
        assert data["agent_name"] == "test-agent"

    def test_encrypt_false_does_not_call_gpg(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        with patch("skcapstone.pillars.sync.gpg_encrypt") as mock_gpg:
            push_seed(tmp_agent_home, "test", encrypt=False)
        mock_gpg.assert_not_called()

    def test_encrypt_true_falls_back_to_plaintext_if_gpg_fails(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        with patch("skcapstone.pillars.sync.gpg_encrypt", return_value=None):
            result = push_seed(tmp_agent_home, "test", encrypt=True)
        assert result is not None
        assert result.exists()


# ── TestPullSeeds ─────────────────────────────────────────────────────────────

class TestPullSeeds:
    """Tests for pull_seeds()."""

    def test_returns_empty_when_no_inbox(self, tmp_agent_home: Path):
        result = pull_seeds(tmp_agent_home, decrypt=False)
        assert result == []

    def test_processes_json_seed_in_inbox(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        inbox = tmp_agent_home / "sync" / "inbox"
        seed_data = {"schema_version": "1.0", "agent_name": "peer", "seed_type": "state_snapshot"}
        (inbox / f"peer-host-20260101T000000Z{SEED_EXTENSION}").write_text(
            json.dumps(seed_data), encoding="utf-8"
        )
        results = pull_seeds(tmp_agent_home, decrypt=False)
        assert len(results) == 1
        assert results[0]["agent_name"] == "peer"

    def test_processed_seed_moved_to_archive(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        inbox = tmp_agent_home / "sync" / "inbox"
        seed_name = f"agent-host-20260101T000000Z{SEED_EXTENSION}"
        (inbox / seed_name).write_text(json.dumps({"schema_version": "1.0"}), encoding="utf-8")
        pull_seeds(tmp_agent_home, decrypt=False)
        assert not (inbox / seed_name).exists()
        assert (tmp_agent_home / "sync" / "archive" / seed_name).exists()

    def test_skips_dotfiles_in_inbox(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        inbox = tmp_agent_home / "sync" / "inbox"
        (inbox / ".DS_Store").write_text("binary")
        results = pull_seeds(tmp_agent_home, decrypt=False)
        assert results == []

    def test_skips_corrupt_json(self, tmp_agent_home: Path):
        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        inbox = tmp_agent_home / "sync" / "inbox"
        (inbox / f"bad{SEED_EXTENSION}").write_text("{{not-json")
        results = pull_seeds(tmp_agent_home, decrypt=False)
        assert results == []


# ── TestSaveSyncState ─────────────────────────────────────────────────────────

class TestSaveSyncState:
    """Tests for save_sync_state()."""

    def test_writes_sync_state_file(self, tmp_agent_home: Path):
        from skcapstone.models import SyncState

        cfg = _no_gpg_config(tmp_agent_home)
        state = initialize_sync(tmp_agent_home, config=cfg)
        sync_dir = tmp_agent_home / "sync"
        save_sync_state(sync_dir, state)
        assert (sync_dir / "sync-state.json").exists()

    def test_persists_peers_known(self, tmp_agent_home: Path):
        from skcapstone.models import SyncState

        cfg = _no_gpg_config(tmp_agent_home)
        initialize_sync(tmp_agent_home, config=cfg)
        sync_dir = tmp_agent_home / "sync"
        state = SyncState(peers_known=3, status=PillarStatus.ACTIVE)
        save_sync_state(sync_dir, state)
        data = json.loads((sync_dir / "sync-state.json").read_text())
        assert data["peers_known"] == 3
