"""
Sovereign Heartbeat v2 — active health beacon for agent meshes.

Each agent node publishes a heartbeat file containing its current
state, capacity, capabilities, and TTL. Syncthing distributes these
files across the mesh. Other agents read heartbeats to understand
the network's health and available resources.

Conflict-free: each node writes only its own file. Stale heartbeats
(past TTL) are marked as offline.

Architecture:
    ~/.skcapstone/heartbeats/
    ├── opus.json         # This node's heartbeat
    ├── lumina.json       # Peer heartbeat (via Syncthing)
    ├── grok.json         # Peer heartbeat
    └── ...

Usage:
    beacon = HeartbeatBeacon(home, agent_name="opus")
    beacon.pulse()                    # Publish heartbeat
    peers = beacon.discover_peers()   # Find live peers
    health = beacon.mesh_health()     # Network overview
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.heartbeat")

DEFAULT_TTL_SECONDS = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class HeartbeatService(BaseModel):
    """A backend service advertised in the heartbeat."""

    name: str        # "qdrant", "falkordb"
    port: int        # 6333, 6379
    protocol: str = "http"  # http, redis


class AgentCapability(BaseModel):
    """A single agent capability."""

    name: str
    version: str = "1.0"
    enabled: bool = True


class NodeCapacity(BaseModel):
    """Resource capacity of a node."""

    cpu_count: int = 0
    memory_total_mb: int = 0
    memory_available_mb: int = 0
    disk_free_gb: float = 0.0
    gpu_available: bool = False
    gpu_name: str = ""


class Heartbeat(BaseModel):
    """A single agent heartbeat beacon."""

    agent_name: str
    status: str = "alive"  # alive, busy, draining, offline
    hostname: str = ""
    platform: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    ttl_seconds: int = DEFAULT_TTL_SECONDS
    uptime_hours: float = 0.0

    # Agent state
    soul_active: str = ""
    claimed_tasks: list[str] = Field(default_factory=list)
    loaded_model: str = ""
    session_active: bool = False

    # Resources
    capacity: NodeCapacity = Field(default_factory=NodeCapacity)

    # Capabilities
    capabilities: list[AgentCapability] = Field(default_factory=list)

    # Metadata
    version: str = ""
    fingerprint: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Service advertisement (optional — old heartbeats without these still parse)
    services: list[HeartbeatService] = Field(default_factory=list)
    tailscale_ip: str = ""

    @property
    def is_alive(self) -> bool:
        """Whether this heartbeat is still valid (within TTL)."""
        expires = self.timestamp + timedelta(seconds=self.ttl_seconds)
        return datetime.now(timezone.utc) <= expires

    @property
    def age_seconds(self) -> float:
        """Seconds since this heartbeat was published."""
        delta = datetime.now(timezone.utc) - self.timestamp
        return delta.total_seconds()


class PeerInfo(BaseModel):
    """Summary of a discovered peer."""

    agent_name: str
    status: str
    alive: bool
    age_seconds: float
    hostname: str = ""
    capabilities: list[str] = Field(default_factory=list)
    soul_active: str = ""
    claimed_tasks: int = 0
    services: list[str] = Field(default_factory=list)
    tailscale_ip: str = ""


class MeshHealth(BaseModel):
    """Health summary of the agent mesh."""

    total_peers: int = 0
    alive_peers: int = 0
    offline_peers: int = 0
    busy_peers: int = 0
    total_capabilities: list[str] = Field(default_factory=list)
    peers: list[PeerInfo] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# HeartbeatBeacon
# ---------------------------------------------------------------------------


class HeartbeatBeacon:
    """Active health beacon for sovereign agent meshes.

    Publishes heartbeats, discovers peers, and reports mesh health.

    Args:
        home: Agent home directory (~/.skcapstone).
        agent_name: Name of the local agent.
        ttl_seconds: Heartbeat TTL before considered stale.
    """

    def __init__(
        self,
        home: Path,
        agent_name: str = "anonymous",
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._home = home
        self._agent = agent_name
        self._ttl = ttl_seconds
        self._heartbeat_dir = home / "heartbeats"
        self._start_time = datetime.now(timezone.utc)

    def initialize(self) -> None:
        """Create the heartbeat directory."""
        self._heartbeat_dir.mkdir(parents=True, exist_ok=True)

    def pulse(
        self,
        status: str = "alive",
        claimed_tasks: Optional[list[str]] = None,
        loaded_model: str = "",
        capabilities: Optional[list[AgentCapability]] = None,
        metadata: Optional[dict[str, Any]] = None,
        services: Optional[list[HeartbeatService]] = None,
        tailscale_ip: Optional[str] = None,
    ) -> Heartbeat:
        """Publish a heartbeat beacon.

        Writes the agent's current state to its heartbeat file.
        Only writes to its own file — never touches peer files.

        Args:
            status: Agent status (alive, busy, draining, offline).
            claimed_tasks: Currently claimed task IDs.
            loaded_model: Currently loaded AI model.
            capabilities: Agent capabilities list.
            metadata: Additional metadata.
            services: Backend services to advertise.  Auto-detected if None.
            tailscale_ip: Tailscale IP address.  Auto-detected if None.

        Returns:
            The published Heartbeat.
        """
        self.initialize()

        uptime = (datetime.now(timezone.utc) - self._start_time).total_seconds() / 3600

        heartbeat = Heartbeat(
            agent_name=self._agent,
            status=status,
            hostname=platform.node(),
            platform=f"{platform.system()} {platform.machine()}",
            ttl_seconds=self._ttl,
            uptime_hours=round(uptime, 2),
            claimed_tasks=claimed_tasks or [],
            loaded_model=loaded_model,
            session_active=True,
            capacity=self._detect_capacity(),
            capabilities=capabilities or self._detect_capabilities(),
            version=self._detect_version(),
            fingerprint=self._detect_fingerprint(),
            soul_active=self._detect_soul(),
            metadata=metadata or {},
            services=services if services is not None else self._detect_services(),
            tailscale_ip=tailscale_ip if tailscale_ip is not None else self._detect_tailscale_ip(),
        )

        path = self._heartbeat_dir / f"{self._agent}.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(
            heartbeat.model_dump_json(indent=2),
            encoding="utf-8",
        )
        tmp_path.rename(path)

        logger.debug("Heartbeat pulse: %s status=%s", self._agent, status)
        return heartbeat

    def read_heartbeat(self, agent_name: str) -> Optional[Heartbeat]:
        """Read a specific agent's heartbeat.

        Args:
            agent_name: The agent to read.

        Returns:
            Heartbeat or None if not found.
        """
        path = self._heartbeat_dir / f"{agent_name}.json"
        if not path.exists():
            return None
        try:
            return Heartbeat.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            logger.warning("Cannot read heartbeat for %s: %s", agent_name, exc)
            return None

    def discover_peers(self, include_self: bool = False) -> list[PeerInfo]:
        """Discover all peers from heartbeat files.

        Args:
            include_self: Whether to include own heartbeat.

        Returns:
            List of PeerInfo for all discovered agents.
        """
        self.initialize()
        peers: list[PeerInfo] = []

        for f in sorted(self._heartbeat_dir.glob("*.json")):
            if f.name.endswith(".tmp"):
                continue

            agent_name = f.stem
            if not include_self and agent_name == self._agent:
                continue

            try:
                hb = Heartbeat.model_validate_json(
                    f.read_text(encoding="utf-8")
                )
                peers.append(PeerInfo(
                    agent_name=hb.agent_name,
                    status=hb.status if hb.is_alive else "offline",
                    alive=hb.is_alive,
                    age_seconds=round(hb.age_seconds, 1),
                    hostname=hb.hostname,
                    capabilities=[c.name for c in hb.capabilities if c.enabled],
                    soul_active=hb.soul_active,
                    claimed_tasks=len(hb.claimed_tasks),
                    services=[s.name for s in hb.services],
                    tailscale_ip=hb.tailscale_ip,
                ))
            except Exception as exc:
                logger.warning("Cannot parse heartbeat %s: %s", f.name, exc)

        return peers

    def mesh_health(self) -> MeshHealth:
        """Get overall mesh health summary.

        Returns:
            MeshHealth with peer counts and capability overview.
        """
        peers = self.discover_peers(include_self=True)
        alive = [p for p in peers if p.alive]
        offline = [p for p in peers if not p.alive]
        busy = [p for p in peers if p.status == "busy"]

        all_caps: set[str] = set()
        for p in alive:
            all_caps.update(p.capabilities)

        return MeshHealth(
            total_peers=len(peers),
            alive_peers=len(alive),
            offline_peers=len(offline),
            busy_peers=len(busy),
            total_capabilities=sorted(all_caps),
            peers=peers,
        )

    def find_capable(self, capability: str) -> list[PeerInfo]:
        """Find alive peers with a specific capability.

        Args:
            capability: The capability name to search for.

        Returns:
            List of alive peers with the capability.
        """
        peers = self.discover_peers(include_self=True)
        return [
            p for p in peers
            if p.alive and capability in p.capabilities
        ]

    def mark_offline(self) -> None:
        """Mark this agent as going offline.

        Publishes a final heartbeat with status "offline" and
        TTL of 0 so peers know immediately.
        """
        self.pulse(status="offline")

    # -------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------

    def _detect_capacity(self) -> NodeCapacity:
        """Detect current node resource capacity."""
        try:
            cpu_count = os.cpu_count() or 0
            disk = shutil.disk_usage(self._home)
            disk_free_gb = round(disk.free / (1024 ** 3), 1)

            mem_total = 0
            mem_avail = 0
            try:
                import psutil
                mem = psutil.virtual_memory()
                mem_total = mem.total // (1024 * 1024)
                mem_avail = mem.available // (1024 * 1024)
            except ImportError:
                # Fallback: read from /proc/meminfo on Linux
                meminfo = Path("/proc/meminfo")
                if meminfo.exists():
                    for line in meminfo.read_text().splitlines():
                        if line.startswith("MemTotal:"):
                            mem_total = int(line.split()[1]) // 1024
                        elif line.startswith("MemAvailable:"):
                            mem_avail = int(line.split()[1]) // 1024

            gpu_available = False
            gpu_name = ""
            if shutil.which("nvidia-smi"):
                gpu_available = True
                gpu_name = "nvidia"

            return NodeCapacity(
                cpu_count=cpu_count,
                memory_total_mb=mem_total,
                memory_available_mb=mem_avail,
                disk_free_gb=disk_free_gb,
                gpu_available=gpu_available,
                gpu_name=gpu_name,
            )
        except Exception:
            return NodeCapacity()

    def _detect_capabilities(self) -> list[AgentCapability]:
        """Detect available capabilities from installed packages."""
        caps: list[AgentCapability] = []
        cap_checks = [
            ("skcapstone", "skcapstone"),
            ("skmemory", "skmemory"),
            ("skchat", "skchat"),
            ("skcomm", "skcomm"),
            ("capauth", "capauth"),
            ("cloud9", "cloud9"),
        ]
        for name, module in cap_checks:
            try:
                __import__(module)
                caps.append(AgentCapability(name=name))
            except ImportError:
                pass
        return caps

    def _detect_version(self) -> str:
        """Detect skcapstone version."""
        try:
            from . import __version__
            return __version__
        except Exception:
            return "unknown"

    def _detect_fingerprint(self) -> str:
        """Detect agent identity fingerprint."""
        identity_path = self._home / "identity" / "identity.json"
        if identity_path.exists():
            try:
                data = json.loads(identity_path.read_text(encoding="utf-8"))
                return data.get("fingerprint", "")[:16]
            except Exception:
                pass
        return ""

    def _detect_soul(self) -> str:
        """Detect active soul overlay."""
        active_path = self._home / "soul" / "active.json"
        if active_path.exists():
            try:
                data = json.loads(active_path.read_text(encoding="utf-8"))
                return data.get("active_soul", "")
            except Exception:
                pass
        return ""

    def _detect_services(self) -> list[HeartbeatService]:
        """Auto-detect locally running backend services.

        Checks if well-known ports are listening on localhost.

        Returns:
            List of detected HeartbeatService entries.
        """
        import socket as _socket

        services: list[HeartbeatService] = []
        checks = [
            ("qdrant", 6333, "http"),
            ("falkordb", 6379, "redis"),
        ]

        for name, port, protocol in checks:
            try:
                s = _socket.create_connection(("127.0.0.1", port), timeout=1)
                s.close()
                services.append(HeartbeatService(
                    name=name, port=port, protocol=protocol,
                ))
            except (OSError, _socket.timeout):
                pass

        return services

    def _detect_tailscale_ip(self) -> str:
        """Best-effort Tailscale IP detection.

        Runs ``tailscale status --json`` and extracts the self IP.
        Fails silently if Tailscale is not installed or not running.

        Returns:
            Tailscale IPv4 address or empty string.
        """
        import subprocess

        try:
            result = subprocess.run(
                ["tailscale", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                ts_ips = data.get("Self", {}).get("TailscaleIPs", [])
                # Prefer IPv4
                for ip in ts_ips:
                    if "." in ip:
                        return ip
                if ts_ips:
                    return ts_ips[0]
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
            pass
        return ""
