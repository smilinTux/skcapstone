"""
mDNS peer discovery for SKCapstone — Zeroconf-based LAN peer detection.

Registers ``_skcapstone._tcp`` on daemon start and browses for other
instances on the local network.  Discovered peers are written as synthetic
heartbeat files (``metadata.source = "mdns"``) so
``HeartbeatBeacon.discover_peers()`` picks them up through the normal flow.

Gracefully disabled at import time if the ``zeroconf`` package is not
installed — no hard dependency.
"""

from __future__ import annotations

import json
import logging
import platform as _platform
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.mdns_discovery")

MDNS_SERVICE_TYPE = "_skcapstone._tcp.local."

# Short TTL so a stale mDNS peer auto-expires within 2 minutes if the
# browse callback never fires ``remove_service``.
MDNS_TTL = 120

try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf  # type: ignore[import-untyped]

    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False


class MDNSDiscovery:
    """mDNS peer discovery for the SKCapstone agent mesh.

    Registers the local agent as a ``_skcapstone._tcp`` Zeroconf service and
    browses for other instances on the same LAN segment.  When a peer is
    found, a synthetic heartbeat JSON file is written to *heartbeats_dir* with
    ``metadata.source = "mdns"`` so ``HeartbeatBeacon.discover_peers()``
    transparently includes it.  When the service disappears the heartbeat is
    marked offline (TTL=0) so it immediately ages out.

    Args:
        agent_name: Local agent name — used as the mDNS service instance name
            and written into the synthetic heartbeat ``agent_name`` field.
        port: Local HTTP API port advertised in the TXT record.
        heartbeats_dir: Directory that ``HeartbeatBeacon`` reads heartbeat
            files from (``~/.skcapstone/heartbeats/`` by default).
    """

    def __init__(
        self,
        agent_name: str,
        port: int,
        heartbeats_dir: Path,
    ) -> None:
        self._agent_name = agent_name
        self._port = port
        self._heartbeats_dir = heartbeats_dir

        self._zc: Optional[object] = None       # Zeroconf instance
        self._browser: Optional[object] = None  # ServiceBrowser
        self._info: Optional[object] = None     # ServiceInfo

        self._lock = threading.Lock()
        # Maps raw mDNS service name → agent_name for peers we track
        self._mdns_peers: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Register the local service and start browsing for peers.

        Does nothing (logs a warning) when ``zeroconf`` is not installed.
        """
        if not _ZEROCONF_AVAILABLE:
            logger.warning(
                "zeroconf not installed — mDNS peer discovery disabled. "
                "Install with: pip install 'skcapstone[mdns]'"
            )
            return

        self._heartbeats_dir.mkdir(parents=True, exist_ok=True)

        zc = Zeroconf()
        self._zc = zc

        addresses = self._local_addresses()
        props: dict[str, str] = {
            "agent": self._agent_name,
            "platform": f"{_platform.system()} {_platform.machine()}",
        }
        instance_name = f"{self._agent_name}.{MDNS_SERVICE_TYPE}"
        self._info = ServiceInfo(
            type_=MDNS_SERVICE_TYPE,
            name=instance_name,
            addresses=addresses,
            port=self._port,
            properties=props,
            server=f"{socket.gethostname()}.local.",
        )

        try:
            zc.register_service(self._info)
            logger.info(
                "mDNS: registered '%s' on port %d", instance_name, self._port
            )
        except Exception as exc:
            logger.warning("mDNS: service registration failed: %s", exc)

        self._browser = ServiceBrowser(zc, MDNS_SERVICE_TYPE, self._make_listener())
        logger.info("mDNS: browsing for %s", MDNS_SERVICE_TYPE)

    def stop(self) -> None:
        """Unregister the local service and close the Zeroconf socket."""
        if self._zc is None:
            return

        try:
            if self._info is not None:
                self._zc.unregister_service(self._info)  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("mDNS: unregister error: %s", exc)

        try:
            self._zc.close()  # type: ignore[attr-defined]
        except Exception as exc:
            logger.debug("mDNS: close error: %s", exc)

        self._zc = None
        logger.info("mDNS: stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_listener(self):
        """Build a zeroconf ServiceListener that delegates to this instance."""
        discovery = self

        class _Listener:
            def add_service(self, zc, type_: str, name: str) -> None:
                discovery._on_add(zc, type_, name)

            def remove_service(self, zc, type_: str, name: str) -> None:
                discovery._on_remove(zc, type_, name)

            def update_service(self, zc, type_: str, name: str) -> None:
                discovery._on_add(zc, type_, name)

        return _Listener()

    def _on_add(self, zc, type_: str, name: str) -> None:
        """Handle a newly discovered or updated ``_skcapstone._tcp`` service."""
        try:
            info = zc.get_service_info(type_, name)
            if info is None:
                return

            props: dict[str, str] = {}
            for k, v in (info.properties or {}).items():
                key = k.decode() if isinstance(k, bytes) else str(k)
                val = v.decode() if isinstance(v, bytes) else str(v)
                props[key] = val

            agent_name = props.get("agent", name.split(".")[0])

            # Skip ourselves — our own registration fires the browser too
            if agent_name == self._agent_name:
                return

            addresses = [
                socket.inet_ntoa(a) if len(a) == 4 else a.hex()
                for a in (info.addresses or [])
            ]

            logger.info(
                "mDNS: discovered peer '%s' at %s:%d",
                agent_name,
                addresses,
                info.port,
            )
            self._write_mdns_heartbeat(agent_name, addresses, info.port, props)

            with self._lock:
                self._mdns_peers[name] = agent_name

        except Exception as exc:
            logger.warning("mDNS: error handling add for %s: %s", name, exc)

    def _on_remove(self, zc, type_: str, name: str) -> None:
        """Handle a ``_skcapstone._tcp`` service going offline."""
        with self._lock:
            agent_name = self._mdns_peers.pop(name, None)

        if agent_name is None:
            return

        logger.info("mDNS: peer '%s' left the LAN", agent_name)
        self._write_mdns_heartbeat(agent_name, [], 0, {}, offline=True)

    def _write_mdns_heartbeat(
        self,
        agent_name: str,
        addresses: list[str],
        port: int,
        props: dict[str, str],
        offline: bool = False,
    ) -> None:
        """Write (or update) a synthetic heartbeat JSON for an mDNS peer.

        Existing heartbeat files whose ``metadata.source`` is **not** ``mdns``
        are left untouched so a real Syncthing-synced heartbeat is never
        overwritten by a weaker mDNS-sourced one.
        """
        self._heartbeats_dir.mkdir(parents=True, exist_ok=True)

        safe_name = agent_name.lower().replace(" ", "-")
        path = self._heartbeats_dir / f"{safe_name}.json"

        # Guard: do not overwrite a real (non-mDNS) heartbeat
        if path.exists() and not offline:
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("metadata", {}).get("source") != "mdns":
                    logger.debug(
                        "mDNS: skipping heartbeat write for '%s' — "
                        "a non-mDNS heartbeat already exists",
                        agent_name,
                    )
                    return
            except Exception as exc:
                logger.warning("Failed to read existing mDNS heartbeat for %s: %s", agent_name, exc)

        heartbeat = {
            "agent_name": agent_name,
            "status": "offline" if offline else "alive",
            "hostname": props.get("hostname", ""),
            "platform": props.get("platform", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ttl_seconds": 0 if offline else MDNS_TTL,
            "uptime_hours": 0.0,
            "soul_active": "",
            "claimed_tasks": [],
            "loaded_model": "",
            "session_active": False,
            "consciousness_active": False,
            "uptime_seconds": 0.0,
            "cpu_load_1min": 0.0,
            "memory_used_mb": 0,
            "active_conversations": 0,
            "messages_processed_24h": 0,
            "capacity": {
                "cpu_count": 0,
                "memory_total_mb": 0,
                "memory_available_mb": 0,
                "disk_free_gb": 0.0,
                "gpu_available": False,
                "gpu_name": "",
            },
            "capabilities": [],
            "version": "",
            "fingerprint": "",
            "metadata": {
                "source": "mdns",
                "addresses": addresses,
                "port": port,
            },
            "services": [],
            "tailscale_ip": "",
        }

        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(heartbeat, indent=2), encoding="utf-8")
        tmp.rename(path)
        logger.debug(
            "mDNS: wrote heartbeat for '%s' (offline=%s)", agent_name, offline
        )

    @staticmethod
    def _local_addresses() -> list[bytes]:
        """Return non-loopback local IPv4 addresses as packed 4-byte values.

        Falls back to ``127.0.0.1`` if no suitable address is found so that
        the service can still be registered (useful for loopback-only testing).
        """
        addrs: list[bytes] = []
        try:
            hostname = socket.gethostname()
            for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(
                hostname, None, socket.AF_INET
            ):
                ip: str = sockaddr[0]
                if not ip.startswith("127."):
                    packed = socket.inet_aton(ip)
                    if packed not in addrs:
                        addrs.append(packed)
        except Exception as exc:
            logger.debug("mDNS: address detection failed: %s", exc)

        if not addrs:
            addrs = [socket.inet_aton("127.0.0.1")]

        return addrs
