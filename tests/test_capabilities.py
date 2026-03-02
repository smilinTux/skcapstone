"""Tests for agent capability advertisement.

Covers:
- AgentConfig defaults
- HeartbeatBeacon reading capabilities from config
- HeartbeatBeacon falling back to defaults when config absent
- Heartbeat pulse includes config capabilities
- CLI capabilities command (list, add, remove)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.heartbeat import AgentCapability, HeartbeatBeacon
from skcapstone.models import AgentConfig


# ---------------------------------------------------------------------------
# AgentConfig defaults
# ---------------------------------------------------------------------------


class TestAgentConfigCapabilities:
    """AgentConfig capabilities field."""

    def test_default_capabilities(self) -> None:
        """AgentConfig has the four default capabilities."""
        cfg = AgentConfig()
        assert cfg.capabilities == ["consciousness", "code", "chat", "memory"]

    def test_custom_capabilities(self) -> None:
        """AgentConfig accepts custom capabilities."""
        cfg = AgentConfig(capabilities=["vector-search", "reasoning"])
        assert cfg.capabilities == ["vector-search", "reasoning"]

    def test_capabilities_preserved_in_yaml_roundtrip(self, tmp_path: Path) -> None:
        """Capabilities survive a YAML serialise/deserialise round-trip."""
        cfg = AgentConfig(capabilities=["consciousness", "code"])
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.dump({"capabilities": cfg.capabilities}, default_flow_style=False),
            encoding="utf-8",
        )
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        restored = AgentConfig(**loaded)
        assert restored.capabilities == ["consciousness", "code"]


# ---------------------------------------------------------------------------
# HeartbeatBeacon — capability loading
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Minimal agent home."""
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "identity.json").write_text(
        json.dumps({"name": "opus", "fingerprint": "ABCD1234567890AB"}),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def beacon(home: Path) -> HeartbeatBeacon:
    b = HeartbeatBeacon(home, agent_name="opus")
    b.initialize()
    return b


class TestHeartbeatCapabilityLoading:
    """HeartbeatBeacon reads capabilities from config."""

    def test_defaults_when_no_config(self, beacon: HeartbeatBeacon) -> None:
        """Falls back to AgentConfig defaults when config.yaml is absent."""
        caps = beacon._load_config_capabilities()
        assert caps == ["consciousness", "code", "chat", "memory"]

    def test_reads_capabilities_from_config(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Reads custom capabilities list from config.yaml."""
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.dump({"capabilities": ["consciousness", "code", "vector-search"]}),
            encoding="utf-8",
        )
        caps = beacon._load_config_capabilities()
        assert caps == ["consciousness", "code", "vector-search"]

    def test_detect_capabilities_includes_config_caps(
        self, beacon: HeartbeatBeacon, home: Path
    ) -> None:
        """_detect_capabilities() includes all config-listed capabilities."""
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.dump({"capabilities": ["consciousness", "code", "chat", "memory"]}),
            encoding="utf-8",
        )
        caps = beacon._detect_capabilities()
        names = [c.name for c in caps]
        assert "consciousness" in names
        assert "code" in names
        assert "chat" in names
        assert "memory" in names

    def test_detect_capabilities_no_duplicates_from_packages(
        self, beacon: HeartbeatBeacon, home: Path
    ) -> None:
        """Packages already in config list are not duplicated."""
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.dump({"capabilities": ["skcapstone"]}),
            encoding="utf-8",
        )
        caps = beacon._detect_capabilities()
        names = [c.name for c in caps]
        assert names.count("skcapstone") == 1

    def test_pulse_heartbeat_has_config_capabilities(
        self, beacon: HeartbeatBeacon, home: Path
    ) -> None:
        """Pulse embeds config capabilities in the published heartbeat."""
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.dump({"capabilities": ["consciousness", "code", "chat", "memory"]}),
            encoding="utf-8",
        )
        hb = beacon.pulse()
        cap_names = [c.name for c in hb.capabilities]
        assert "consciousness" in cap_names
        assert "code" in cap_names
        assert "chat" in cap_names
        assert "memory" in cap_names

    def test_pulse_heartbeat_persisted_capabilities(
        self, beacon: HeartbeatBeacon, home: Path
    ) -> None:
        """Capabilities written to disk match what was pulsed."""
        config_dir = home / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.dump({"capabilities": ["consciousness", "chat"]}),
            encoding="utf-8",
        )
        beacon.pulse()
        hb_file = home / "heartbeats" / "opus.json"
        data = json.loads(hb_file.read_text(encoding="utf-8"))
        persisted_names = [c["name"] for c in data["capabilities"]]
        assert "consciousness" in persisted_names
        assert "chat" in persisted_names


# ---------------------------------------------------------------------------
# CLI capabilities command
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestCapabilitiesCLI:
    """CLI: skcapstone capabilities."""

    def test_capabilities_list_defaults(self, runner: CliRunner, tmp_path: Path) -> None:
        """capabilities list shows defaults when no config present."""
        from skcapstone.cli import main

        result = runner.invoke(main, ["capabilities", "list", "--home", str(tmp_path)])
        assert result.exit_code == 0
        assert "consciousness" in result.output
        assert "code" in result.output
        assert "chat" in result.output
        assert "memory" in result.output

    def test_capabilities_list_json(self, runner: CliRunner, tmp_path: Path) -> None:
        """capabilities list --json returns a JSON array."""
        from skcapstone.cli import main

        result = runner.invoke(
            main, ["capabilities", "list", "--json", "--home", str(tmp_path)]
        )
        assert result.exit_code == 0
        caps = json.loads(result.output)
        assert isinstance(caps, list)
        assert "consciousness" in caps

    def test_capabilities_add(self, runner: CliRunner, tmp_path: Path) -> None:
        """capabilities add appends a new capability to config."""
        from skcapstone.cli import main

        result = runner.invoke(
            main, ["capabilities", "add", "vector-search", "--home", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "vector-search" in result.output

        # Verify it persisted
        config_path = tmp_path / "config" / "config.yaml"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "vector-search" in data["capabilities"]

    def test_capabilities_add_no_duplicate(self, runner: CliRunner, tmp_path: Path) -> None:
        """capabilities add skips silently when capability already present."""
        from skcapstone.cli import main

        # Add a new capability first (ensures config file is created)
        runner.invoke(main, ["capabilities", "add", "vector-search", "--home", str(tmp_path)])
        config_path = tmp_path / "config" / "config.yaml"
        data1 = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        count_before = data1["capabilities"].count("vector-search")

        # Add the same capability again — should NOT duplicate
        result = runner.invoke(
            main, ["capabilities", "add", "vector-search", "--home", str(tmp_path)]
        )
        assert result.exit_code == 0
        data2 = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert data2["capabilities"].count("vector-search") == count_before

    def test_capabilities_remove(self, runner: CliRunner, tmp_path: Path) -> None:
        """capabilities remove drops a capability from config."""
        from skcapstone.cli import main

        # First add so it's persisted
        runner.invoke(main, ["capabilities", "add", "chat", "--home", str(tmp_path)])

        result = runner.invoke(
            main, ["capabilities", "remove", "chat", "--home", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "chat" in result.output

        config_path = tmp_path / "config" / "config.yaml"
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert "chat" not in data["capabilities"]

    def test_capabilities_root_command_shows_table(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """skcapstone capabilities (no subcommand) prints capability table."""
        from skcapstone.cli import main

        result = runner.invoke(main, ["capabilities", "--home", str(tmp_path)])
        assert result.exit_code == 0
        assert "consciousness" in result.output
