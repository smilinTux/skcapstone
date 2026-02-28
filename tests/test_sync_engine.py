"""
Unit tests for skcapstone SyncEngine.

Covers orchestration of push/pull across backends, config/state
persistence, backend filtering, and the init_sync factory.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    home = tmp_path / ".skcapstone"
    home.mkdir()
    for d in ("identity", "memory", "trust", "config", "skills"):
        (home / d).mkdir()

    (home / "identity" / "identity.json").write_text(
        json.dumps({
            "name": "EngineTestAgent",
            "fingerprint": "FFFF1111AAAA2222BBBB3333CCCC4444DDDD5555",
        })
    )
    (home / "trust" / "trust.json").write_text(
        json.dumps({"depth": 3.0, "trust_level": 0.7})
    )
    (home / "config" / "config.yaml").write_text("agent_name: EngineTestAgent\n")
    (home / "manifest.json").write_text(
        json.dumps({"name": "EngineTestAgent", "version": "0.1.0", "connectors": []})
    )
    for layer in ("short-term", "mid-term", "long-term"):
        (home / "memory" / layer).mkdir(parents=True)
    (home / "memory" / "long-term" / "mem.json").write_text(
        json.dumps({"content": "remember this"})
    )
    return home


@pytest.fixture
def engine(agent_home: Path):
    from skcapstone.sync.engine import SyncEngine

    e = SyncEngine(agent_home)
    e.config.encrypt = False
    return e


@pytest.fixture
def local_backend_config(tmp_path: Path):
    from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

    backup = tmp_path / "local-bkp"
    backup.mkdir()
    return SyncBackendConfig(
        backend_type=SyncBackendType.LOCAL,
        local_path=backup,
    )


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


class TestSyncEngineInit:
    def test_default_config_is_empty(self, engine):
        """Fresh engine has no backends configured."""
        assert engine.config.backends == []

    def test_state_starts_at_zero(self, engine):
        """Fresh engine state has zero push/pull counts."""
        assert engine.state.push_count == 0
        assert engine.state.pull_count == 0
        assert engine.state.last_push is None
        assert engine.state.last_pull is None

    def test_creates_sync_directory(self, agent_home: Path):
        """Engine constructor creates agent_home/sync/."""
        from skcapstone.sync.engine import SyncEngine

        SyncEngine(agent_home)
        assert (agent_home / "sync").is_dir()

    def test_loads_existing_config(self, agent_home: Path):
        """Engine reads backends from an existing config.yaml."""
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config_data = {
            "backends": [{"backend_type": "syncthing", "enabled": True}],
            "encrypt": False,
            "auto_push": True,
            "auto_pull": True,
        }
        (agent_home / "sync").mkdir(exist_ok=True)
        (agent_home / "sync" / "config.yaml").write_text(
            yaml.dump(config_data, default_flow_style=False)
        )

        from skcapstone.sync.engine import SyncEngine

        eng = SyncEngine(agent_home)
        assert len(eng.config.backends) == 1
        assert eng.config.backends[0].backend_type == SyncBackendType.SYNCTHING

    def test_loads_existing_state(self, agent_home: Path):
        """Engine reads push/pull counts from an existing state.json."""
        (agent_home / "sync").mkdir(exist_ok=True)
        (agent_home / "sync" / "state.json").write_text(
            json.dumps({"push_count": 5, "pull_count": 3})
        )

        from skcapstone.sync.engine import SyncEngine

        eng = SyncEngine(agent_home)
        assert eng.state.push_count == 5
        assert eng.state.pull_count == 3

    def test_corrupt_config_falls_back_to_defaults(self, agent_home: Path):
        """Corrupt config.yaml results in default SyncConfig."""
        (agent_home / "sync").mkdir(exist_ok=True)
        (agent_home / "sync" / "config.yaml").write_text("{{{NOT YAML")

        from skcapstone.sync.engine import SyncEngine

        eng = SyncEngine(agent_home)
        assert eng.config.backends == []

    def test_corrupt_state_falls_back_to_defaults(self, agent_home: Path):
        """Corrupt state.json results in default SyncState."""
        (agent_home / "sync").mkdir(exist_ok=True)
        (agent_home / "sync" / "state.json").write_text("NOT JSON")

        from skcapstone.sync.engine import SyncEngine

        eng = SyncEngine(agent_home)
        assert eng.state.push_count == 0


# ---------------------------------------------------------------------------
# add_backend / save_config
# ---------------------------------------------------------------------------


class TestAddBackend:
    def test_add_backend_appends(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        assert len(engine.config.backends) == 1

    def test_add_backend_replaces_same_type(self, engine, tmp_path: Path):
        """Adding a backend of the same type replaces the existing one."""
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        first = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=tmp_path / "first",
        )
        (tmp_path / "first").mkdir()
        second = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=tmp_path / "second",
        )
        (tmp_path / "second").mkdir()

        engine.add_backend(first)
        engine.add_backend(second)
        # Only one LOCAL backend should remain
        local_backends = [
            b for b in engine.config.backends
            if b.backend_type.value == "local"
        ]
        assert len(local_backends) == 1
        assert local_backends[0].local_path == tmp_path / "second"

    def test_add_backend_persists_to_disk(self, engine, local_backend_config, agent_home: Path):
        engine.add_backend(local_backend_config)
        config_file = agent_home / "sync" / "config.yaml"
        assert config_file.exists()
        data = yaml.safe_load(config_file.read_text())
        assert len(data["backends"]) == 1

    def test_config_reloaded_by_new_engine(self, agent_home: Path, local_backend_config):
        from skcapstone.sync.engine import SyncEngine

        eng1 = SyncEngine(agent_home)
        eng1.config.encrypt = False
        eng1.add_backend(local_backend_config)

        eng2 = SyncEngine(agent_home)
        assert len(eng2.config.backends) == 1


# ---------------------------------------------------------------------------
# push
# ---------------------------------------------------------------------------


class TestSyncEnginePush:
    def test_push_local_backend_succeeds(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        results = engine.push()
        assert results.get("local") is True

    def test_push_increments_push_count(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()
        assert engine.state.push_count == 1

    def test_push_sets_last_push_backend(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()
        assert engine.state.last_push_backend == "local"

    def test_push_sets_last_push_timestamp(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()
        assert engine.state.last_push is not None

    def test_push_persists_state(self, engine, agent_home: Path, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()
        state_file = agent_home / "sync" / "state.json"
        assert state_file.exists()
        data = json.loads(state_file.read_text())
        assert data["push_count"] == 1

    def test_push_skips_disabled_backend(self, engine, tmp_path: Path):
        """Backends with enabled=False should be skipped."""
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        backup = tmp_path / "disabled-bkp"
        backup.mkdir()
        disabled = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=backup,
            enabled=False,
        )
        engine.add_backend(disabled)
        results = engine.push()
        assert "local" not in results

    def test_push_skips_unavailable_backend(self, engine, agent_home: Path):
        """Unavailable backends are reported as False in results."""
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        config = SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING)
        engine.add_backend(config)

        with patch("shutil.which", return_value=None):
            results = engine.push()

        assert results.get("syncthing") is False

    def test_push_with_backend_filter(self, engine, tmp_path: Path):
        """backend_filter should push only to the named backend."""
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        backup = tmp_path / "bkp"
        backup.mkdir()
        engine.add_backend(SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING))
        engine.add_backend(SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL, local_path=backup
        ))

        with patch("shutil.which", return_value=None):
            results = engine.push(backend_filter="local")

        assert "local" in results
        assert "syncthing" not in results

    def test_push_no_backends_returns_empty(self, engine):
        results = engine.push()
        assert results == {}

    def test_push_multiple_backends_all_succeed(self, engine, tmp_path: Path):
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        for i in range(2):
            d = tmp_path / f"bkp{i}"
            d.mkdir()
            engine.add_backend(SyncBackendConfig(
                backend_type=SyncBackendType.LOCAL, local_path=d
            ))
        # Second add_backend replaces first (same type), so only 1 local backend
        results = engine.push()
        assert "local" in results


# ---------------------------------------------------------------------------
# pull
# ---------------------------------------------------------------------------


class TestSyncEnginePull:
    def _push_to_local(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()

    def test_pull_returns_none_with_no_backends(self, engine):
        assert engine.pull() is None

    def test_pull_restores_state(self, engine, local_backend_config, tmp_path: Path):
        from skcapstone.sync.engine import SyncEngine

        self._push_to_local(engine, local_backend_config)

        restore_home = tmp_path / "restore"
        restore_home.mkdir()
        engine2 = SyncEngine(restore_home)
        engine2.config.encrypt = False
        engine2.add_backend(local_backend_config)
        result = engine2.pull()
        assert result is not None

    def test_pull_increments_pull_count(self, engine, local_backend_config, tmp_path: Path):
        from skcapstone.sync.engine import SyncEngine

        self._push_to_local(engine, local_backend_config)

        restore = tmp_path / "restore"
        restore.mkdir()
        engine2 = SyncEngine(restore)
        engine2.config.encrypt = False
        engine2.add_backend(local_backend_config)
        engine2.pull()
        assert engine2.state.pull_count == 1

    def test_pull_sets_last_pull_backend(self, engine, local_backend_config, tmp_path: Path):
        from skcapstone.sync.engine import SyncEngine

        self._push_to_local(engine, local_backend_config)

        restore = tmp_path / "restore"
        restore.mkdir()
        engine2 = SyncEngine(restore)
        engine2.config.encrypt = False
        engine2.add_backend(local_backend_config)
        engine2.pull()
        assert engine2.state.last_pull_backend == "local"

    def test_pull_dry_run_does_not_extract(self, engine, local_backend_config, tmp_path: Path):
        """dry_run=True downloads the vault but doesn't call unpack."""
        from skcapstone.sync.engine import SyncEngine

        self._push_to_local(engine, local_backend_config)

        restore = tmp_path / "restore"
        restore.mkdir()
        engine2 = SyncEngine(restore)
        engine2.config.encrypt = False
        engine2.add_backend(local_backend_config)

        with patch.object(engine2.vault, "unpack") as mock_unpack:
            result = engine2.pull(dry_run=True)

        assert result is not None
        mock_unpack.assert_not_called()

    def test_pull_skips_disabled_backend(self, engine, tmp_path: Path):
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        backup = tmp_path / "bkp"
        backup.mkdir()
        disabled = SyncBackendConfig(
            backend_type=SyncBackendType.LOCAL,
            local_path=backup,
            enabled=False,
        )
        engine.add_backend(disabled)
        result = engine.pull()
        assert result is None

    def test_pull_with_backend_filter(self, engine, local_backend_config, tmp_path: Path):
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        self._push_to_local(engine, local_backend_config)

        restore = tmp_path / "restore"
        restore.mkdir()
        engine2 = SyncEngine(restore)
        engine2.config.encrypt = False
        engine2.add_backend(local_backend_config)
        # Also add syncthing (unavailable) - filter should pull from local only
        engine2.add_backend(SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING))

        with patch("shutil.which", side_effect=lambda x: None if x == "syncthing" else "/usr/bin/git"):
            result = engine2.pull(backend_filter="local")
        assert result is not None

    def test_pull_skips_unavailable_backend_tries_next(self, engine, local_backend_config, tmp_path: Path):
        """Pull skips unavailable backends and tries the next one."""
        from skcapstone.sync.engine import SyncEngine
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        self._push_to_local(engine, local_backend_config)

        restore = tmp_path / "restore"
        restore.mkdir()
        engine2 = SyncEngine(restore)
        engine2.config.encrypt = False
        # Add syncthing first (unavailable), then local
        engine2.config.backends = [
            SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING),
            local_backend_config,
        ]

        with patch("shutil.which", side_effect=lambda x: None if x == "syncthing" else "/bin/true"):
            result = engine2.pull()
        # Local backend should succeed
        assert result is not None


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


class TestSyncEngineStatus:
    def test_status_keys(self, engine):
        info = engine.status()
        assert "state" in info
        assert "backends" in info
        assert "vaults" in info
        assert "encrypt" in info
        assert "auto_push" in info

    def test_status_reports_backend_availability(self, engine):
        from skcapstone.sync.models import SyncBackendConfig, SyncBackendType

        engine.add_backend(SyncBackendConfig(backend_type=SyncBackendType.SYNCTHING))
        with patch("shutil.which", return_value=None):
            info = engine.status()
        assert info["backends"][0]["available"] is False

    def test_status_vault_count(self, engine, local_backend_config):
        engine.add_backend(local_backend_config)
        engine.push()
        info = engine.status()
        assert info["vaults"] >= 1


# ---------------------------------------------------------------------------
# init_sync factory
# ---------------------------------------------------------------------------


class TestInitSync:
    def test_init_sync_returns_engine(self, agent_home: Path):
        from skcapstone.sync.engine import SyncEngine, init_sync
        from skcapstone.sync.models import SyncBackendType

        eng = init_sync(agent_home, SyncBackendType.SYNCTHING)
        assert isinstance(eng, SyncEngine)

    def test_init_sync_adds_backend(self, agent_home: Path):
        from skcapstone.sync.engine import init_sync
        from skcapstone.sync.models import SyncBackendType

        eng = init_sync(agent_home, SyncBackendType.SYNCTHING)
        assert len(eng.config.backends) == 1
        assert eng.config.backends[0].backend_type == SyncBackendType.SYNCTHING

    def test_init_sync_with_local_backend(self, agent_home: Path, tmp_path: Path):
        from skcapstone.sync.engine import init_sync
        from skcapstone.sync.models import SyncBackendType

        backup = tmp_path / "bkp"
        backup.mkdir()
        eng = init_sync(agent_home, SyncBackendType.LOCAL, local_path=backup)
        assert eng.config.backends[0].backend_type == SyncBackendType.LOCAL
