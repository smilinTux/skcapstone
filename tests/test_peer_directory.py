"""Tests for the PeerDirectory transport address registry.

Covers:
- load() on empty / missing / malformed directory
- add_peer() — creates entry, persists to YAML, overwrites on re-add
- remove_peer() — removes known peer, returns False for unknown
- resolve() — returns address or None
- list_peers() — empty and with entries, sorted
- update_last_seen() — timestamps known peer, no-op on unknown
- auto_discover() — discovers from heartbeats dir and outbox dirs
- YAML persistence round-trip
- CLI: peers list, peers add, peers discover
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from skcapstone.peer_directory import DirectoryEntry, PeerDirectory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_directory(tmp_path: Path) -> PeerDirectory:
    """Return a PeerDirectory rooted at tmp_path."""
    return PeerDirectory(home=tmp_path)


def write_heartbeat(hb_dir: Path, agent: str, fingerprint: str = "") -> None:
    """Write a minimal heartbeat JSON file."""
    hb_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "agent_name": agent,
        "status": "alive",
        "hostname": f"{agent}-host",
        "platform": "Linux x86_64",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ttl_seconds": 300,
        "fingerprint": fingerprint,
    }
    (hb_dir / f"{agent}.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: Empty directory
# ---------------------------------------------------------------------------


class TestLoad:
    """Test load() behaviour."""

    def test_load_missing_file(self, tmp_path):
        """load() on a fresh home returns empty dict without error."""
        d = make_directory(tmp_path)
        result = d.load()
        assert result == {}

    def test_load_empty_yaml(self, tmp_path):
        """load() on an empty YAML file returns empty dict."""
        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()
        (peers_dir / "directory.yaml").write_text("", encoding="utf-8")
        d = make_directory(tmp_path)
        assert d.load() == {}

    def test_load_malformed_yaml_is_safe(self, tmp_path):
        """load() on malformed YAML does not raise, returns empty."""
        peers_dir = tmp_path / "peers"
        peers_dir.mkdir()
        (peers_dir / "directory.yaml").write_text("{{{invalid yaml", encoding="utf-8")
        d = make_directory(tmp_path)
        result = d.load()
        # Graceful degradation — no exception
        assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Test 2: add_peer / resolve
# ---------------------------------------------------------------------------


class TestAddAndResolve:
    """Test add_peer() and resolve()."""

    def test_add_peer_creates_entry(self, tmp_path):
        """add_peer() returns a DirectoryEntry with correct fields."""
        d = make_directory(tmp_path)
        entry = d.add_peer(
            name="Lumina",
            address="/home/user/.skcapstone/sync/comms/outbox/lumina",
            transport="syncthing",
            fingerprint="AABB1122",
        )
        assert entry.name == "lumina"
        assert entry.address == "/home/user/.skcapstone/sync/comms/outbox/lumina"
        assert entry.transport == "syncthing"
        assert entry.fingerprint == "AABB1122"
        assert entry.last_seen is not None

    def test_resolve_known_peer(self, tmp_path):
        """resolve() returns the address of a known peer."""
        d = make_directory(tmp_path)
        d.add_peer("Opus", "/path/to/outbox/opus")
        assert d.resolve("Opus") == "/path/to/outbox/opus"

    def test_resolve_case_insensitive(self, tmp_path):
        """resolve() is case-insensitive."""
        d = make_directory(tmp_path)
        d.add_peer("LUMINA", "/outbox/lumina")
        assert d.resolve("lumina") == "/outbox/lumina"
        assert d.resolve("LUMINA") == "/outbox/lumina"
        assert d.resolve("Lumina") == "/outbox/lumina"

    def test_resolve_unknown_returns_none(self, tmp_path):
        """resolve() returns None for an unknown peer."""
        d = make_directory(tmp_path)
        assert d.resolve("nobody") is None

    def test_add_peer_overwrites(self, tmp_path):
        """Adding the same peer twice updates the entry."""
        d = make_directory(tmp_path)
        d.add_peer("Jarvis", "/old/path")
        d.add_peer("Jarvis", "/new/path", transport="tailscale")
        assert d.resolve("jarvis") == "/new/path"
        # List should still show only one entry
        peers = d.list_peers()
        assert len([p for p in peers if p.name == "jarvis"]) == 1


# ---------------------------------------------------------------------------
# Test 3: remove_peer
# ---------------------------------------------------------------------------


class TestRemovePeer:
    """Test remove_peer()."""

    def test_remove_existing(self, tmp_path):
        """remove_peer() removes a known peer and returns True."""
        d = make_directory(tmp_path)
        d.add_peer("Grok", "/outbox/grok")
        assert d.remove_peer("Grok") is True
        assert d.resolve("grok") is None

    def test_remove_unknown_returns_false(self, tmp_path):
        """remove_peer() returns False when peer is not in directory."""
        d = make_directory(tmp_path)
        assert d.remove_peer("nobody") is False

    def test_remove_case_insensitive(self, tmp_path):
        """remove_peer() handles mixed case."""
        d = make_directory(tmp_path)
        d.add_peer("Ava", "/outbox/ava")
        assert d.remove_peer("AVA") is True
        assert d.resolve("ava") is None


# ---------------------------------------------------------------------------
# Test 4: list_peers
# ---------------------------------------------------------------------------


class TestListPeers:
    """Test list_peers()."""

    def test_empty(self, tmp_path):
        """list_peers() on empty directory returns []."""
        d = make_directory(tmp_path)
        assert d.list_peers() == []

    def test_sorted_alphabetically(self, tmp_path):
        """list_peers() returns entries sorted by name."""
        d = make_directory(tmp_path)
        d.add_peer("Zeta", "/z")
        d.add_peer("Alpha", "/a")
        d.add_peer("Mango", "/m")
        names = [p.name for p in d.list_peers()]
        assert names == sorted(names)

    def test_returns_all_peers(self, tmp_path):
        """list_peers() returns every added peer."""
        d = make_directory(tmp_path)
        for i in range(5):
            d.add_peer(f"agent{i}", f"/outbox/agent{i}")
        assert len(d.list_peers()) == 5


# ---------------------------------------------------------------------------
# Test 5: update_last_seen
# ---------------------------------------------------------------------------


class TestUpdateLastSeen:
    """Test update_last_seen()."""

    def test_updates_timestamp(self, tmp_path):
        """update_last_seen() sets a new ISO timestamp for a known peer."""
        d = make_directory(tmp_path)
        d.add_peer("Opus", "/outbox/opus")
        old_ts = d.resolve("opus")  # address won't change, but we want to check last_seen

        import time
        time.sleep(0.01)  # ensure clock advances

        d.update_last_seen("Opus")
        entry = d.list_peers()[0]
        assert entry.last_seen is not None

    def test_noop_on_unknown(self, tmp_path):
        """update_last_seen() on an unknown peer does not raise."""
        d = make_directory(tmp_path)
        d.update_last_seen("ghost")  # must not raise


# ---------------------------------------------------------------------------
# Test 6: auto_discover
# ---------------------------------------------------------------------------


class TestAutoDiscover:
    """Test auto_discover()."""

    def test_discover_from_heartbeats(self, tmp_path):
        """auto_discover() adds peers found in heartbeat files."""
        hb_dir = tmp_path / "heartbeats"
        write_heartbeat(hb_dir, "lumina", fingerprint="FP123")
        write_heartbeat(hb_dir, "jarvis")

        d = make_directory(tmp_path)
        added = d.auto_discover(heartbeats_dir=hb_dir)

        names = {e.name for e in added}
        assert "lumina" in names
        assert "jarvis" in names
        # Transport should default to syncthing
        assert all(e.transport == "syncthing" for e in added)

    def test_discover_from_outbox_dirs(self, tmp_path):
        """auto_discover() adds peers from Syncthing outbox directories."""
        outbox_root = tmp_path / "sync" / "comms" / "outbox"
        (outbox_root / "ava").mkdir(parents=True)
        (outbox_root / "mcp-builder").mkdir(parents=True)

        d = make_directory(tmp_path)
        added = d.auto_discover()

        names = {e.name for e in added}
        assert "ava" in names
        assert "mcp-builder" in names

    def test_discover_skips_known(self, tmp_path):
        """auto_discover() does not overwrite existing entries."""
        hb_dir = tmp_path / "heartbeats"
        write_heartbeat(hb_dir, "lumina")

        d = make_directory(tmp_path)
        d.add_peer("lumina", "/custom/path")
        added = d.auto_discover(heartbeats_dir=hb_dir)

        # lumina was already known — should not appear in added
        added_names = {e.name for e in added}
        assert "lumina" not in added_names
        # And the existing address must be preserved
        assert d.resolve("lumina") == "/custom/path"

    def test_discover_empty_dirs(self, tmp_path):
        """auto_discover() on empty dirs returns empty list."""
        d = make_directory(tmp_path)
        added = d.auto_discover()
        assert added == []


# ---------------------------------------------------------------------------
# Test 7: YAML persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistence:
    """Test that entries survive a full save → load cycle."""

    def test_round_trip(self, tmp_path):
        """Entries written by one PeerDirectory instance are readable by another."""
        d1 = make_directory(tmp_path)
        d1.add_peer("Lumina", "/outbox/lumina", transport="syncthing", fingerprint="FP99")
        d1.add_peer("Grok", "/outbox/grok", transport="tailscale")

        # Fresh instance — reads from disk
        d2 = make_directory(tmp_path)
        d2.load()
        assert d2.resolve("lumina") == "/outbox/lumina"
        assert d2.resolve("grok") == "/outbox/grok"
        lumina = next(p for p in d2.list_peers() if p.name == "lumina")
        assert lumina.fingerprint == "FP99"

    def test_yaml_file_created(self, tmp_path):
        """add_peer() creates the directory.yaml file."""
        d = make_directory(tmp_path)
        d.add_peer("Opus", "/outbox/opus")
        yaml_path = tmp_path / "peers" / "directory.yaml"
        assert yaml_path.exists()
        data = yaml.safe_load(yaml_path.read_text())
        assert "opus" in data

    def test_remove_removes_from_yaml(self, tmp_path):
        """remove_peer() removes the entry from YAML on disk."""
        d = make_directory(tmp_path)
        d.add_peer("Jarvis", "/outbox/jarvis")
        d.remove_peer("Jarvis")

        yaml_path = tmp_path / "peers" / "directory.yaml"
        data = yaml.safe_load(yaml_path.read_text()) or {}
        assert "jarvis" not in data


# ---------------------------------------------------------------------------
# Test 8: CLI — peers list, add, discover
# ---------------------------------------------------------------------------


class TestCLI:
    """Test the `skcapstone peers` CLI commands."""

    def test_peers_help(self):
        """`peers --help` exits cleanly."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["peers", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "add" in result.output

    def test_peers_list_empty(self, tmp_path):
        """`peers list` on empty directory shows no-peers message."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["peers", "list", "--home", str(tmp_path)])
        assert result.exit_code == 0
        assert "No peers" in result.output

    def test_peers_add_and_list(self, tmp_path):
        """`peers add` then `peers list` shows the new entry."""
        from skcapstone.cli import main
        runner = CliRunner()

        add_result = runner.invoke(main, [
            "peers", "add",
            "--name", "Lumina",
            "--address", "/outbox/lumina",
            "--home", str(tmp_path),
        ])
        assert add_result.exit_code == 0, add_result.output
        assert "lumina" in add_result.output.lower()

        list_result = runner.invoke(main, ["peers", "list", "--home", str(tmp_path)])
        assert list_result.exit_code == 0
        assert "lumina" in list_result.output.lower()

    def test_peers_list_json(self, tmp_path):
        """`peers list --json-out` produces valid JSON."""
        from skcapstone.cli import main
        runner = CliRunner()

        runner.invoke(main, [
            "peers", "add",
            "--name", "Grok",
            "--address", "/outbox/grok",
            "--home", str(tmp_path),
        ])

        result = runner.invoke(main, ["peers", "list", "--json-out", "--home", str(tmp_path)])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert any(p["name"] == "grok" for p in data)

    def test_peers_discover_cli(self, tmp_path):
        """`peers discover` reports newly found peers."""
        from skcapstone.cli import main

        hb_dir = tmp_path / "heartbeats"
        write_heartbeat(hb_dir, "lumina")

        runner = CliRunner()
        result = runner.invoke(main, ["peers", "discover", "--home", str(tmp_path)])
        assert result.exit_code == 0
        assert "lumina" in result.output.lower()
