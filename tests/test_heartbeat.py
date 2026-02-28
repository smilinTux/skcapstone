"""Tests for Sovereign Heartbeat v2."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from skcapstone.heartbeat import (
    AgentCapability,
    Heartbeat,
    HeartbeatBeacon,
    MeshHealth,
    NodeCapacity,
    PeerInfo,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home."""
    (tmp_path / "identity").mkdir()
    (tmp_path / "identity" / "identity.json").write_text(json.dumps({
        "name": "opus",
        "fingerprint": "ABCD1234567890AB",
    }), encoding="utf-8")
    return tmp_path


@pytest.fixture
def beacon(home: Path) -> HeartbeatBeacon:
    """Create an initialized HeartbeatBeacon."""
    b = HeartbeatBeacon(home, agent_name="opus")
    b.initialize()
    return b


def _write_peer_heartbeat(
    home: Path,
    agent_name: str,
    status: str = "alive",
    ttl_seconds: int = 300,
    age_seconds: float = 0,
    capabilities: list[dict] | None = None,
) -> None:
    """Helper to create a peer heartbeat file."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    hb = {
        "agent_name": agent_name,
        "status": status,
        "hostname": f"{agent_name}-host",
        "platform": "Linux x86_64",
        "timestamp": ts.isoformat(),
        "ttl_seconds": ttl_seconds,
        "uptime_hours": 1.0,
        "capabilities": capabilities or [],
        "claimed_tasks": [],
        "capacity": {},
    }
    hb_dir = home / "heartbeats"
    hb_dir.mkdir(parents=True, exist_ok=True)
    (hb_dir / f"{agent_name}.json").write_text(
        json.dumps(hb, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for heartbeat setup."""

    def test_initialize_creates_dir(self, home: Path) -> None:
        """Initialize creates heartbeats directory."""
        b = HeartbeatBeacon(home)
        b.initialize()
        assert (home / "heartbeats").is_dir()

    def test_initialize_idempotent(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Multiple initializations don't break anything."""
        beacon.initialize()
        beacon.initialize()
        assert (home / "heartbeats").is_dir()


# ---------------------------------------------------------------------------
# Pulse
# ---------------------------------------------------------------------------


class TestPulse:
    """Tests for heartbeat publishing."""

    def test_pulse_creates_file(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Pulse creates the agent's heartbeat file."""
        beacon.pulse()
        assert (home / "heartbeats" / "opus.json").exists()

    def test_pulse_returns_heartbeat(self, beacon: HeartbeatBeacon) -> None:
        """Pulse returns a Heartbeat object."""
        hb = beacon.pulse()
        assert hb.agent_name == "opus"
        assert hb.status == "alive"
        assert hb.is_alive is True

    def test_pulse_with_status(self, beacon: HeartbeatBeacon) -> None:
        """Pulse accepts custom status."""
        hb = beacon.pulse(status="busy")
        assert hb.status == "busy"

    def test_pulse_with_tasks(self, beacon: HeartbeatBeacon) -> None:
        """Pulse tracks claimed tasks."""
        hb = beacon.pulse(claimed_tasks=["task1", "task2"])
        assert hb.claimed_tasks == ["task1", "task2"]

    def test_pulse_with_model(self, beacon: HeartbeatBeacon) -> None:
        """Pulse tracks loaded model."""
        hb = beacon.pulse(loaded_model="claude-opus-4-6")
        assert hb.loaded_model == "claude-opus-4-6"

    def test_pulse_detects_capacity(self, beacon: HeartbeatBeacon) -> None:
        """Pulse detects node capacity."""
        hb = beacon.pulse()
        assert hb.capacity.cpu_count > 0

    def test_pulse_detects_fingerprint(self, beacon: HeartbeatBeacon) -> None:
        """Pulse reads identity fingerprint."""
        hb = beacon.pulse()
        assert hb.fingerprint == "ABCD1234567890AB"

    def test_pulse_atomic_write(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Pulse uses atomic write (no .tmp left)."""
        beacon.pulse()
        tmp = home / "heartbeats" / "opus.json.tmp"
        assert not tmp.exists()

    def test_pulse_with_capabilities(self, beacon: HeartbeatBeacon) -> None:
        """Pulse accepts custom capabilities."""
        caps = [AgentCapability(name="code-review", version="2.0")]
        hb = beacon.pulse(capabilities=caps)
        assert len(hb.capabilities) == 1
        assert hb.capabilities[0].name == "code-review"


# ---------------------------------------------------------------------------
# Read heartbeat
# ---------------------------------------------------------------------------


class TestReadHeartbeat:
    """Tests for reading heartbeats."""

    def test_read_own_heartbeat(self, beacon: HeartbeatBeacon) -> None:
        """Read own heartbeat after pulse."""
        beacon.pulse()
        hb = beacon.read_heartbeat("opus")
        assert hb is not None
        assert hb.agent_name == "opus"

    def test_read_peer_heartbeat(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Read a peer's heartbeat."""
        _write_peer_heartbeat(home, "lumina")
        hb = beacon.read_heartbeat("lumina")
        assert hb is not None
        assert hb.agent_name == "lumina"

    def test_read_nonexistent(self, beacon: HeartbeatBeacon) -> None:
        """Reading nonexistent heartbeat returns None."""
        assert beacon.read_heartbeat("ghost") is None


# ---------------------------------------------------------------------------
# Discover peers
# ---------------------------------------------------------------------------


class TestDiscoverPeers:
    """Tests for peer discovery."""

    def test_discover_empty(self, beacon: HeartbeatBeacon) -> None:
        """Empty mesh returns no peers."""
        peers = beacon.discover_peers()
        assert peers == []

    def test_discover_excludes_self(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Discovery excludes own heartbeat by default."""
        beacon.pulse()
        _write_peer_heartbeat(home, "lumina")
        peers = beacon.discover_peers()
        names = [p.agent_name for p in peers]
        assert "lumina" in names
        assert "opus" not in names

    def test_discover_includes_self(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Discovery can include own heartbeat."""
        beacon.pulse()
        peers = beacon.discover_peers(include_self=True)
        names = [p.agent_name for p in peers]
        assert "opus" in names

    def test_discover_marks_stale_offline(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Stale heartbeats are marked as offline."""
        _write_peer_heartbeat(home, "stale-agent", ttl_seconds=60, age_seconds=120)
        peers = beacon.discover_peers()
        stale = next(p for p in peers if p.agent_name == "stale-agent")
        assert stale.alive is False
        assert stale.status == "offline"

    def test_discover_live_peers(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Live heartbeats are correctly identified."""
        _write_peer_heartbeat(home, "live-agent", ttl_seconds=300, age_seconds=10)
        peers = beacon.discover_peers()
        live = next(p for p in peers if p.agent_name == "live-agent")
        assert live.alive is True
        assert live.status == "alive"

    def test_discover_with_capabilities(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Peer capabilities are included in discovery."""
        _write_peer_heartbeat(
            home, "capable-agent",
            capabilities=[{"name": "code-review", "enabled": True}],
        )
        peers = beacon.discover_peers()
        cap = next(p for p in peers if p.agent_name == "capable-agent")
        assert "code-review" in cap.capabilities


# ---------------------------------------------------------------------------
# Mesh health
# ---------------------------------------------------------------------------


class TestMeshHealth:
    """Tests for mesh health reporting."""

    def test_mesh_health_empty(self, beacon: HeartbeatBeacon) -> None:
        """Empty mesh health."""
        health = beacon.mesh_health()
        assert health.total_peers == 0
        assert health.alive_peers == 0

    def test_mesh_health_mixed(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Mixed mesh with alive and stale peers."""
        beacon.pulse()
        _write_peer_heartbeat(home, "lumina", age_seconds=10)
        _write_peer_heartbeat(home, "stale", ttl_seconds=60, age_seconds=120)

        health = beacon.mesh_health()
        assert health.total_peers == 3  # opus + lumina + stale
        assert health.alive_peers == 2
        assert health.offline_peers == 1

    def test_mesh_health_capabilities(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Mesh health aggregates capabilities."""
        _write_peer_heartbeat(
            home, "agent-a",
            capabilities=[{"name": "code-review", "enabled": True}],
        )
        _write_peer_heartbeat(
            home, "agent-b",
            capabilities=[{"name": "deployment", "enabled": True}],
        )

        health = beacon.mesh_health()
        assert "code-review" in health.total_capabilities
        assert "deployment" in health.total_capabilities


# ---------------------------------------------------------------------------
# Find capable
# ---------------------------------------------------------------------------


class TestFindCapable:
    """Tests for capability-based peer search."""

    def test_find_capable_peers(self, beacon: HeartbeatBeacon, home: Path) -> None:
        """Find peers with a specific capability."""
        _write_peer_heartbeat(
            home, "reviewer",
            capabilities=[{"name": "code-review", "enabled": True}],
        )
        _write_peer_heartbeat(
            home, "deployer",
            capabilities=[{"name": "deployment", "enabled": True}],
        )

        reviewers = beacon.find_capable("code-review")
        assert len(reviewers) == 1
        assert reviewers[0].agent_name == "reviewer"

    def test_find_capable_none(self, beacon: HeartbeatBeacon) -> None:
        """No peers with capability returns empty."""
        assert beacon.find_capable("nonexistent") == []


# ---------------------------------------------------------------------------
# Mark offline
# ---------------------------------------------------------------------------


class TestMarkOffline:
    """Tests for offline marking."""

    def test_mark_offline(self, beacon: HeartbeatBeacon) -> None:
        """Mark offline publishes offline status."""
        beacon.mark_offline()
        hb = beacon.read_heartbeat("opus")
        assert hb is not None
        assert hb.status == "offline"


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for heartbeat models."""

    def test_heartbeat_defaults(self) -> None:
        """Heartbeat has sensible defaults."""
        hb = Heartbeat(agent_name="test")
        assert hb.status == "alive"
        assert hb.is_alive is True
        assert hb.ttl_seconds == 300

    def test_heartbeat_expired(self) -> None:
        """Expired heartbeat detected."""
        hb = Heartbeat(
            agent_name="old",
            timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            ttl_seconds=60,
        )
        assert hb.is_alive is False

    def test_node_capacity_defaults(self) -> None:
        """NodeCapacity has sensible defaults."""
        cap = NodeCapacity()
        assert cap.cpu_count == 0
        assert cap.gpu_available is False

    def test_peer_info_defaults(self) -> None:
        """PeerInfo has sensible defaults."""
        p = PeerInfo(
            agent_name="test", status="alive",
            alive=True, age_seconds=10,
        )
        assert p.capabilities == []
        assert p.claimed_tasks == 0
