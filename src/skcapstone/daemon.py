"""
SKCapstone Daemon — the always-on sovereign agent.

Runs as a background process, continuously polling for
incoming messages, scheduling vault sync, monitoring
transport health, and exposing a local HTTP API for
connectors to query agent state.

This is what turns a CLI tool into a living agent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import re
import signal
import struct
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import AGENT_HOME, SHARED_ROOT
from . import activity as _activity

logger = logging.getLogger("skcapstone.daemon")

_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer(peer: str) -> str:
    """Sanitize a peer name for safe filesystem use (path-traversal prevention).

    Strips path separators, null bytes, and characters outside the safe set.
    Returns empty string if the result would be empty.
    """
    if not peer or not isinstance(peer, str):
        return ""
    sanitized = peer.replace("\x00", "").replace("/", "").replace("\\", "")
    sanitized = _PEER_NAME_SAFE_RE.sub("", sanitized)
    sanitized = sanitized.strip(".")
    return sanitized[:64]

DEFAULT_PORT = 7777
PID_FILE = "daemon.pid"
LOG_DIR = "logs"


def _sd_notify(state: str) -> bool:
    """Send a notification to systemd via the NOTIFY_SOCKET.

    Implements the sd_notify(3) protocol using a raw AF_UNIX datagram socket
    so we don't need an external dependency.  Returns True if the notification
    was sent, False if NOTIFY_SOCKET is not set (i.e. not running under systemd).

    Common states:
        "READY=1"       — service startup complete
        "WATCHDOG=1"    — watchdog keep-alive ping
        "STOPPING=1"    — graceful shutdown in progress
    """
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    import socket as _socket
    try:
        sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_DGRAM)
        try:
            if addr[0] == "@":
                addr = "\0" + addr[1:]
            sock.sendto(state.encode("utf-8"), addr)
        finally:
            sock.close()
        return True
    except OSError as exc:
        logger.debug("sd_notify(%r) failed: %s", state, exc)
        return False

# ── WebSocket helpers (RFC 6455, stdlib-only) ─────────────────────────────────

_WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(key: str) -> str:
    """Return the Sec-WebSocket-Accept value for a given client key."""
    raw = hashlib.sha1((key + _WS_MAGIC).encode("utf-8")).digest()
    return base64.b64encode(raw).decode("ascii")


def _ws_encode_frame(payload: bytes) -> bytes:
    """Encode a WebSocket text frame (server→client, no masking)."""
    n = len(payload)
    if n < 126:
        return struct.pack("BB", 0x81, n) + payload
    if n < 65536:
        return struct.pack("!BBH", 0x81, 126, n) + payload
    return struct.pack("!BBQ", 0x81, 127, n) + payload


def _ws_encode_close() -> bytes:
    """Return a WebSocket close frame."""
    return struct.pack("BB", 0x88, 0)


def _ws_recv_exact(sock, n: int):
    """Read exactly n bytes from sock; return bytes or None on EOF."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _ws_read_frame(sock):
    """Read one WebSocket frame from sock.

    Returns (opcode, payload) or None on EOF.
    Raises TimeoutError on socket timeout, OSError on other errors.
    """
    header = _ws_recv_exact(sock, 2)
    if header is None:
        return None
    b0, b1 = header[0], header[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F
    if length == 126:
        ext = _ws_recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _ws_recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]
    if masked:
        mask = _ws_recv_exact(sock, 4)
        if mask is None:
            return None
        raw = _ws_recv_exact(sock, length) if length else b""
        if raw is None:
            return None
        data = bytearray(raw)
        for i in range(len(data)):
            data[i] ^= mask[i % 4]
        return opcode, bytes(data)
    raw = _ws_recv_exact(sock, length) if length else b""
    if raw is None:
        return None
    return opcode, raw


SHUTDOWN_STATE_FILE = "shutdown_state.json"

# ── Component health tracking ─────────────────────────────────────────────────


class ComponentHealth:
    """Health record for a single daemon subsystem component.

    Tracks status, heartbeat timestamps, and restart history in a thread-safe way.

    Args:
        name: Component identifier (e.g. "poll", "consciousness").
        auto_restart: Whether the watchdog should auto-restart this component.
        heartbeat_timeout: Seconds without a heartbeat before marking dead.
    """

    def __init__(
        self,
        name: str,
        *,
        auto_restart: bool = False,
        heartbeat_timeout: int = 120,
    ):
        self.name = name
        self.auto_restart = auto_restart
        self.heartbeat_timeout = heartbeat_timeout
        self.status: str = "pending"
        self.started_at: Optional[datetime] = None
        self.last_heartbeat: Optional[datetime] = None
        self.restart_count: int = 0
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()

    def mark_started(self) -> None:
        """Transition to alive and record start time."""
        with self._lock:
            self.status = "alive"
            now = datetime.now(timezone.utc)
            self.started_at = now
            self.last_heartbeat = now

    def pulse(self) -> None:
        """Record a heartbeat — component is alive and working."""
        with self._lock:
            self.last_heartbeat = datetime.now(timezone.utc)
            if self.status != "alive":
                self.status = "alive"

    def mark_dead(self, error: str = "") -> None:
        """Transition to dead, optionally recording the error."""
        with self._lock:
            self.status = "dead"
            if error:
                self.last_error = error

    def mark_restarting(self) -> None:
        """Transition to restarting and increment the restart counter."""
        with self._lock:
            self.status = "restarting"
            self.restart_count += 1

    def mark_disabled(self) -> None:
        """Mark component as permanently disabled (not started)."""
        with self._lock:
            self.status = "disabled"

    def mark_alive(self) -> None:
        """Mark a passive component as alive (no auto-restart)."""
        with self._lock:
            self.status = "alive"
            if not self.started_at:
                self.started_at = datetime.now(timezone.utc)
            self.last_heartbeat = datetime.now(timezone.utc)

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot of this component's health.

        Returns:
            Dict with name, status, timestamps, restart_count, last_error.
        """
        with self._lock:
            age: Optional[int] = None
            if self.last_heartbeat:
                age = round(
                    (datetime.now(timezone.utc) - self.last_heartbeat).total_seconds()
                )
            return {
                "name": self.name,
                "status": self.status,
                "auto_restart": self.auto_restart,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "last_heartbeat": (
                    self.last_heartbeat.isoformat() if self.last_heartbeat else None
                ),
                "heartbeat_age_seconds": age,
                "restart_count": self.restart_count,
                "last_error": self.last_error,
            }


class ComponentManager:
    """Tracks health and auto-restarts daemon subsystem components.

    Each restartable component is registered with a loop callable.  A watchdog
    thread periodically checks liveness and restarts any component whose thread
    has exited or whose heartbeat has timed out.

    Args:
        stop_event: Shared stop event — when set the watchdog exits cleanly.
    """

    WATCHDOG_INTERVAL = 30  # seconds between watchdog checks
    MAX_RESTARTS = 5  # maximum auto-restart attempts per component

    def __init__(self, stop_event: threading.Event):
        self._stop_event = stop_event
        self._health: dict[str, ComponentHealth] = {}
        self._factories: dict[str, callable] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def register(
        self,
        name: str,
        target: callable,
        *,
        disabled: bool = False,
        heartbeat_timeout: int = 120,
    ) -> ComponentHealth:
        """Register a restartable component loop.

        Args:
            name: Unique component identifier.
            target: Callable that implements the component's loop (runs until
                    stop_event is set).
            disabled: If True, mark as disabled and do not start.
            heartbeat_timeout: Seconds without a heartbeat before the watchdog
                               considers the component dead.

        Returns:
            The ComponentHealth tracker for this component.
        """
        comp = ComponentHealth(name, auto_restart=True, heartbeat_timeout=heartbeat_timeout)
        if disabled:
            comp.mark_disabled()
        with self._lock:
            self._health[name] = comp
            self._factories[name] = target
        return comp

    def register_passive(self, name: str, *, status: str = "alive") -> ComponentHealth:
        """Register a non-restartable component (e.g. consciousness, scheduler).

        These are tracked for status display but not auto-restarted because they
        manage their own internal threads.

        Args:
            name: Unique component identifier.
            status: Initial status ("alive", "disabled", "dead").

        Returns:
            The ComponentHealth tracker.
        """
        comp = ComponentHealth(name, auto_restart=False)
        comp.status = status
        if status == "alive":
            comp.started_at = datetime.now(timezone.utc)
            comp.last_heartbeat = datetime.now(timezone.utc)
        with self._lock:
            self._health[name] = comp
        return comp

    def heartbeat(self, name: str) -> None:
        """Signal that a component is alive. Call this inside component loops.

        Args:
            name: Component identifier.
        """
        with self._lock:
            comp = self._health.get(name)
        if comp:
            comp.pulse()

    def mark_dead(self, name: str, error: str = "") -> None:
        """Explicitly mark a component dead.

        Args:
            name: Component identifier.
            error: Optional error message.
        """
        with self._lock:
            comp = self._health.get(name)
        if comp:
            comp.mark_dead(error)

    def mark_alive(self, name: str) -> None:
        """Mark a passive component alive (e.g. after successful load).

        Args:
            name: Component identifier.
        """
        with self._lock:
            comp = self._health.get(name)
        if comp:
            comp.mark_alive()

    def mark_disabled(self, name: str) -> None:
        """Mark a component as disabled.

        Args:
            name: Component identifier.
        """
        with self._lock:
            comp = self._health.get(name)
        if comp:
            comp.mark_disabled()

    def start_all(self) -> list:
        """Start all registered non-disabled components and the watchdog.

        Returns:
            List of started threading.Thread objects.
        """
        with self._lock:
            names = list(self._health.keys())
            factories = dict(self._factories)

        threads = []
        for name in names:
            with self._lock:
                comp = self._health.get(name)
            if comp and comp.status != "disabled" and name in factories:
                t = self._launch(name, factories[name])
                threads.append(t)

        watchdog = threading.Thread(
            target=self._watchdog_loop,
            name="daemon-watchdog",
            daemon=True,
        )
        watchdog.start()
        threads.append(watchdog)
        return threads

    def _launch(self, name: str, target: callable) -> threading.Thread:
        """Launch a component thread, wrapping it to detect crashes.

        Args:
            name: Component identifier.
            target: Loop callable.

        Returns:
            The started Thread.
        """
        with self._lock:
            comp = self._health.get(name)
        if comp:
            comp.mark_started()

        def _wrapper():
            try:
                target()
            except Exception as exc:
                logger.error("Component '%s' crashed: %s", name, exc)
                with self._lock:
                    c = self._health.get(name)
                if c:
                    c.mark_dead(str(exc))
            else:
                if not self._stop_event.is_set():
                    logger.warning("Component '%s' exited unexpectedly", name)
                    with self._lock:
                        c = self._health.get(name)
                    if c:
                        c.mark_dead("exited unexpectedly")

        t = threading.Thread(target=_wrapper, name=f"daemon-{name}", daemon=True)
        t.start()
        with self._lock:
            self._threads[name] = t
        return t

    def _check_components(self) -> None:
        """Inspect all auto-restart components and restart any that are dead.

        Called by the watchdog loop and also usable directly in tests.
        """
        with self._lock:
            comps = dict(self._health)
            factories = dict(self._factories)
            threads = dict(self._threads)

        for name, comp in comps.items():
            if not comp.auto_restart:
                continue
            if comp.status in ("disabled", "restarting"):
                continue

            t = threads.get(name)
            needs_restart = False

            if comp.status == "dead":
                needs_restart = True
            elif t is not None and not t.is_alive() and comp.status == "alive":
                logger.warning("Component '%s' thread exited", name)
                comp.mark_dead("thread exited")
                needs_restart = True
            elif comp.last_heartbeat:
                age = (
                    datetime.now(timezone.utc) - comp.last_heartbeat
                ).total_seconds()
                if age > comp.heartbeat_timeout:
                    logger.warning(
                        "Component '%s' heartbeat timeout (%.0fs old)", name, age
                    )
                    comp.mark_dead("heartbeat timeout")
                    needs_restart = True

            if not needs_restart:
                continue

            if comp.restart_count >= self.MAX_RESTARTS:
                logger.error(
                    "Component '%s' exceeded max restarts (%d) — giving up",
                    name,
                    self.MAX_RESTARTS,
                )
                continue

            target = factories.get(name)
            if target:
                logger.warning(
                    "Watchdog auto-restarting '%s' (attempt %d/%d)",
                    name,
                    comp.restart_count + 1,
                    self.MAX_RESTARTS,
                )
                comp.mark_restarting()
                self._launch(name, target)

    def _watchdog_loop(self) -> None:
        """Periodically check component health and restart dead components."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.WATCHDOG_INTERVAL)
            if self._stop_event.is_set():
                break
            self._check_components()

    def snapshot(self) -> dict:
        """Return a serializable snapshot of all component health records.

        Returns:
            Dict mapping component name → health snapshot dict.
        """
        with self._lock:
            comps = dict(self._health)
        return {name: comp.snapshot() for name, comp in comps.items()}


class DaemonConfig:
    """Configuration for the daemon process.

    Attributes:
        home: Per-agent home directory.
        shared_root: Shared root for coordination, heartbeats, peers.
        poll_interval: Seconds between inbox polls.
        sync_interval: Seconds between vault sync pushes.
        health_interval: Seconds between transport health checks.
        port: HTTP API port for local queries.
        log_file: Path for daemon log output.
        consciousness_enabled: Whether to start the consciousness loop.
        consciousness_config_path: Optional path to consciousness config.
        tls_enabled: When True the API server uses HTTPS (set via
            ``SKCAPSTONE_TLS=true``).  A self-signed certificate is
            auto-generated under ``~/.skcapstone/tls/`` on first start.
        tls_dir: Directory for TLS certificate and key files.
    """

    def __init__(
        self,
        home: Optional[Path] = None,
        shared_root: Optional[Path] = None,
        poll_interval: int = 10,
        sync_interval: int = 300,
        health_interval: int = 60,
        port: int = DEFAULT_PORT,
        consciousness_enabled: bool = True,
        consciousness_config_path: Optional[Path] = None,
        tls_enabled: Optional[bool] = None,
        tls_dir: Optional[Path] = None,
    ):
        self.home = (home or Path(AGENT_HOME)).expanduser()
        self.shared_root = (shared_root or Path(SHARED_ROOT)).expanduser()
        self.poll_interval = poll_interval
        self.sync_interval = sync_interval
        self.health_interval = health_interval
        self.port = port
        self.consciousness_enabled = consciousness_enabled
        self.consciousness_config_path = consciousness_config_path

        # TLS: env var SKCAPSTONE_TLS=true overrides the constructor arg
        if tls_enabled is None:
            tls_enabled = os.environ.get("SKCAPSTONE_TLS", "").lower() in ("1", "true", "yes")
        self.tls_enabled: bool = tls_enabled
        self.tls_dir: Path = (tls_dir or self.home / "tls").expanduser()

        log_dir = self.home / LOG_DIR
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = log_dir / "daemon.log"


class DaemonState:
    """Thread-safe mutable daemon state.

    Stores the latest results from polling, health checks,
    and sync operations. All access is lock-protected.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at: Optional[datetime] = None
        self.last_poll: Optional[datetime] = None
        self.last_sync: Optional[datetime] = None
        self.last_health: Optional[datetime] = None
        self.messages_received: int = 0
        self.syncs_completed: int = 0
        self.health_reports: dict = {}
        self.errors: list[str] = []
        self.running: bool = False
        self.consciousness_stats: dict = {}
        self.self_healing_report: dict = {}
        self.healing_history: list[dict] = []
        self.inflight_messages: dict[str, dict] = {}
        self.sync_pipeline_status: dict = {}

    def snapshot(self) -> dict:
        """Return a serializable snapshot of current state.

        Returns:
            Dict with all state fields, safe for JSON serialization.
        """
        with self._lock:
            return {
                "running": self.running,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "uptime_seconds": (
                    (datetime.now(timezone.utc) - self.started_at).total_seconds()
                    if self.started_at
                    else 0
                ),
                "last_poll": self.last_poll.isoformat() if self.last_poll else None,
                "last_sync": self.last_sync.isoformat() if self.last_sync else None,
                "last_health": self.last_health.isoformat() if self.last_health else None,
                "messages_received": self.messages_received,
                "syncs_completed": self.syncs_completed,
                "transport_health": self.health_reports,
                "consciousness": self.consciousness_stats,
                "self_healing": self.self_healing_report,
                "self_healing_history": list(self.healing_history[-5:]),
                "sync_pipeline": self.sync_pipeline_status,
                "recent_errors": self.errors[-10:],
                "inflight_count": len(self.inflight_messages),
                "pid": os.getpid(),
            }

    def record_sync_pipeline(self, status: dict) -> None:
        """Record a sync pipeline status snapshot.

        Args:
            status: Dict from :func:`skcapstone.sync_engine.get_sync_pipeline_status`.
        """
        with self._lock:
            self.sync_pipeline_status = status

    def record_poll(self, count: int) -> None:
        """Record an inbox poll result."""
        with self._lock:
            self.last_poll = datetime.now(timezone.utc)
            self.messages_received += count

    def record_sync(self) -> None:
        """Record a successful sync push."""
        with self._lock:
            self.last_sync = datetime.now(timezone.utc)
            self.syncs_completed += 1

    def record_health(self, report: dict) -> None:
        """Record transport health check results."""
        with self._lock:
            self.last_health = datetime.now(timezone.utc)
            self.health_reports = report

    def record_error(self, error: str) -> None:
        """Record an error, keeping only the last 50."""
        with self._lock:
            ts = datetime.now(timezone.utc).isoformat()
            self.errors.append(f"[{ts}] {error}")
            if len(self.errors) > 50:
                self.errors = self.errors[-50:]

    def record_healing_run(self, report: dict) -> None:
        """Record a self-healing run result, keeping the last 20 entries.

        Args:
            report: Healing report dict from SelfHealingDoctor.diagnose_and_heal().
        """
        with self._lock:
            self.self_healing_report = report
            self.healing_history.append(report)
            if len(self.healing_history) > 20:
                self.healing_history = self.healing_history[-20:]

    def add_inflight(self, msg_id: str, data: dict) -> None:
        """Mark a message as in-flight (being processed).

        Args:
            msg_id: Unique message identifier.
            data: Serializable envelope metadata for persistence.
        """
        with self._lock:
            self.inflight_messages[msg_id] = data

    def remove_inflight(self, msg_id: str) -> None:
        """Remove a message from the in-flight set (processing complete).

        Args:
            msg_id: Unique message identifier.
        """
        with self._lock:
            self.inflight_messages.pop(msg_id, None)

    def get_inflight(self) -> list[dict]:
        """Return a snapshot of all currently in-flight message data."""
        with self._lock:
            return list(self.inflight_messages.values())


class DaemonService:
    """The sovereign daemon process.

    Manages background threads for inbox polling, vault sync,
    and transport health monitoring. Exposes an HTTP API for
    local status queries.

    Args:
        config: Daemon configuration.
    """

    def __init__(self, config: DaemonConfig):
        self.config = config
        self.state = DaemonState()
        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._server: Optional[HTTPServer] = None
        self._skcomm = None
        self._runtime = None
        self._consciousness = None
        self._healer = None
        self._beacon = None
        self._scheduler = None
        # WebSocket clients: set of raw sockets for connected /ws clients
        self._ws_clients: set = set()
        self._ws_lock = threading.Lock()
        # Component health manager — populated in start()
        self._component_mgr = ComponentManager(self._stop_event)

    def start(self) -> None:
        """Start the daemon and all background workers.

        Writes a PID file, sets up signal handlers, and starts
        polling, sync, health, and HTTP threads.
        """
        self._write_pid()
        self._setup_logging()
        self._setup_signals()

        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)

        logger.info(
            "Daemon starting — home=%s port=%d poll=%ds sync=%ds",
            self.config.home,
            self.config.port,
            self.config.poll_interval,
            self.config.sync_interval,
        )

        self._run_preflight()
        self._load_components()
        self._load_startup_state()

        # ── Register restartable core loops with the component manager ─────────
        poll_timeout = max(self.config.poll_interval * 3 + 30, 60)
        health_timeout = max(self.config.health_interval * 3 + 30, 60)
        sync_timeout = max(self.config.sync_interval * 3 + 30, 120)

        self._component_mgr.register("poll", self._poll_loop, heartbeat_timeout=poll_timeout)
        self._component_mgr.register("health", self._health_loop, heartbeat_timeout=health_timeout)
        self._component_mgr.register("sync", self._sync_loop, heartbeat_timeout=sync_timeout)
        self._component_mgr.register(
            "housekeeping", self._housekeeping_loop, heartbeat_timeout=7230
        )
        self._component_mgr.register(
            "healing",
            self._healing_loop,
            disabled=not bool(self._healer),
            heartbeat_timeout=930,
        )

        # ── Register passive components (managed externally) ──────────────────
        self._component_mgr.register_passive(
            "consciousness",
            status="alive" if self._consciousness else "disabled",
        )
        self._component_mgr.register_passive(
            "scheduler",
            status="alive" if self._scheduler else "disabled",
        )
        self._component_mgr.register_passive(
            "heartbeat",
            status="alive" if self._beacon else "disabled",
        )

        # Start all registered components (core loops + watchdog)
        component_threads = self._component_mgr.start_all()
        self._threads.extend(component_threads)

        # Start consciousness loop threads (manages own threads internally)
        if self._consciousness:
            consciousness_threads = self._consciousness.start()
            self._threads.extend(consciousness_threads)

        # Start task scheduler (manages its own thread internally)
        if self._scheduler:
            scheduler_thread = self._scheduler.start()
            self._threads.append(scheduler_thread)

        self._start_api_server()

        _sd_notify("READY=1")
        logger.info("Daemon started — PID %d", os.getpid())

    def stop(self) -> None:
        """Gracefully stop the daemon and all workers."""
        _sd_notify("STOPPING=1")
        logger.info("Daemon stopping...")
        self._stop_event.set()
        self.state.running = False

        if self._consciousness:
            try:
                self._consciousness.stop()
            except Exception as exc:
                logger.warning("Consciousness stop error: %s", exc)

        if self._server:
            try:
                self._server.shutdown()
            except Exception as exc:
                logger.warning("API server shutdown error: %s", exc)

        for t in self._threads:
            t.join(timeout=5)

        self._save_shutdown_state()
        self._remove_pid()
        logger.info("Daemon stopped.")

    def run_forever(self) -> None:
        """Block until stop is signaled.

        Typically called after start() in the main process.
        """
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def _run_preflight(self) -> None:
        """Run preflight checks before starting the daemon.

        Logs warnings for non-critical issues and aborts with SystemExit
        if any critical check fails.
        """
        try:
            from .preflight import PreflightChecker
        except ImportError:
            logger.warning("PreflightChecker not available — skipping preflight")
            return

        checker = PreflightChecker(home=self.config.home)
        summary = checker.run_all()

        for check in summary["checks"]:
            name = check["name"]
            status = check["status"]
            msg = check["message"]
            if status == "ok":
                logger.info("preflight [%s] OK — %s", name, msg)
            elif status == "warn":
                logger.warning("preflight [%s] WARN — %s", name, msg)
            else:
                logger.error("preflight [%s] FAIL — %s", name, msg)

        if not summary["ok"]:
            failed = [c for c in summary["checks"] if c["status"] == "fail" and c["critical"]]
            msgs = "; ".join(c["message"] for c in failed)
            logger.error("Preflight FAILED — aborting daemon startup: %s", msgs)
            raise SystemExit(f"Daemon preflight failed: {msgs}")

        if summary["warnings"] or summary["failures"]:
            logger.warning(
                "Preflight complete — %d warning(s), %d non-critical failure(s)",
                summary["warnings"],
                summary["failures"],
            )
        else:
            logger.info("Preflight complete — all checks passed")

    def _load_components(self) -> None:
        """Attempt to load SKComm, AgentRuntime, and ConsciousnessLoop."""
        try:
            from skcomm.core import SKComm
            from .sync_engine import ensure_comms_dirs, get_comms_root

            self._skcomm = SKComm.from_config()
            expected_comms_root = get_comms_root(self.config.shared_root)
            ensure_comms_dirs(self.config.shared_root)
            for transport in self._skcomm.router.transports:
                if getattr(transport, "name", "") == "syncthing" and hasattr(transport, "configure"):
                    transport.configure({"comms_root": str(expected_comms_root)})
            logger.info("SKComm loaded — %d transports", len(self._skcomm.router.transports))
        except ImportError:
            logger.warning("SKComm not installed — inbox polling disabled")
        except Exception as exc:
            logger.warning("SKComm failed to load: %s", exc)
            self.state.record_error(f"SKComm load: {exc}")

        try:
            from .runtime import get_runtime
            self._runtime = get_runtime(self.config.home)
            logger.info("Runtime loaded — agent '%s'", self._runtime.manifest.name)
        except Exception as exc:
            logger.warning("Runtime failed to load: %s", exc)
            self.state.record_error(f"Runtime load: {exc}")

        try:
            from .heartbeat import HeartbeatBeacon
            agent_name = self._runtime.manifest.name if self._runtime else "anonymous"
            self._beacon = HeartbeatBeacon(self.config.home, agent_name)
            logger.info("HeartbeatBeacon initialized for '%s'", agent_name)
        except Exception as exc:
            logger.warning("HeartbeatBeacon failed to init: %s", exc)
            self.state.record_error(f"Heartbeat init: {exc}")

        # Load consciousness loop
        if self.config.consciousness_enabled:
            try:
                from .consciousness_config import load_consciousness_config
                from .consciousness_loop import ConsciousnessLoop

                cli_disabled = not self.config.consciousness_enabled
                c_config = load_consciousness_config(
                    self.config.home,
                    cli_disabled=cli_disabled,
                    config_path=self.config.consciousness_config_path,
                )
                if c_config.enabled:
                    self._consciousness = ConsciousnessLoop(
                        c_config, self.state,
                        home=self.config.home,
                        shared_root=self.config.shared_root,
                    )
                    if self._skcomm:
                        self._consciousness.set_skcomm(self._skcomm)
                    logger.info("Consciousness loop loaded")

                    # Preload Ollama model into RAM so first real message is fast
                    def _ollama_warmup():
                        try:
                            from skseed.llm import ollama_callback
                            cb = ollama_callback(model="llama3.2")
                            cb("warmup")
                            logger.info("Ollama warmup complete — llama3.2 loaded")
                        except Exception as exc:
                            logger.debug("Ollama warmup skipped: %s", exc)

                    threading.Thread(
                        target=_ollama_warmup,
                        name="daemon-ollama-warmup",
                        daemon=True,
                    ).start()
                else:
                    logger.info("Consciousness loop disabled by config")
            except Exception as exc:
                logger.warning("Consciousness loop failed to load: %s", exc)
                self.state.record_error(f"Consciousness load: {exc}")

        # Load self-healing doctor
        try:
            from .self_healing import SelfHealingDoctor
            self._healer = SelfHealingDoctor(
                self.config.home, consciousness_loop=self._consciousness,
            )
            logger.info("Self-healing doctor loaded")
        except Exception as exc:
            logger.warning("Self-healing doctor failed to load: %s", exc)
            self.state.record_error(f"Self-healing load: {exc}")

        # Build task scheduler (beacon + consciousness must be ready first)
        try:
            from .scheduled_tasks import build_scheduler

            # Get sync_watcher from consciousness loop if available
            _sync_watcher = getattr(self._consciousness, "_sync_watcher", None)
            self._scheduler = build_scheduler(
                home=self.config.home,
                stop_event=self._stop_event,
                consciousness_loop=self._consciousness,
                beacon=self._beacon,
                sync_watcher=_sync_watcher,
            )
            logger.info("Task scheduler built — %d task(s)", len(self._scheduler._tasks))
        except Exception as exc:
            logger.warning("Task scheduler failed to build: %s", exc)
            self.state.record_error(f"Scheduler build: {exc}")

    def _poll_loop(self) -> None:
        """Continuously poll SKComm inbox for new messages."""
        while not self._stop_event.is_set():
            self._component_mgr.heartbeat("poll")
            if self._skcomm:
                try:
                    envelopes = self._skcomm.receive()
                    count = len(envelopes)
                    self.state.record_poll(count)
                    if count > 0:
                        logger.info("Received %d message(s)", count)
                        self._process_messages(envelopes)
                except Exception as exc:
                    logger.error("Poll error: %s", exc)
                    self.state.record_error(f"Poll: {exc}")
            else:
                self.state.record_poll(0)

            self._stop_event.wait(timeout=self.config.poll_interval)

    def _health_loop(self) -> None:
        """Periodically check transport health and ping systemd watchdog."""
        while not self._stop_event.is_set():
            self._component_mgr.heartbeat("health")
            _sd_notify("WATCHDOG=1")
            if self._skcomm:
                try:
                    report = self._skcomm.status()
                    transports = report.get("transports", {})
                    serializable = {}
                    for name, health in transports.items():
                        if hasattr(health, "model_dump"):
                            serializable[name] = health.model_dump()
                        elif isinstance(health, dict):
                            serializable[name] = health
                        else:
                            serializable[name] = str(health)
                    self.state.record_health(serializable)
                except Exception as exc:
                    logger.error("Health check error: %s", exc)
                    self.state.record_error(f"Health: {exc}")

            if self._beacon:
                try:
                    c_stats = self._consciousness.stats if self._consciousness else {}
                    conv_dir = self.config.shared_root / "conversations"
                    active_convs = len(list(conv_dir.glob("*.json"))) if conv_dir.exists() else 0
                    self._beacon.pulse(
                        consciousness_active=bool(self._consciousness),
                        active_conversations=active_convs,
                        messages_processed_24h=c_stats.get("messages_processed_24h", 0),
                    )
                    _activity.push("heartbeat.published", {
                        "status": "alive",
                        "consciousness_active": bool(self._consciousness),
                        "active_conversations": active_convs,
                        "messages_processed_24h": c_stats.get("messages_processed_24h", 0),
                    })
                except Exception as exc:
                    logger.warning("Heartbeat pulse failed: %s", exc)

            # Sync pipeline status — inbox/outbox file counts and path alignment
            try:
                from .sync_engine import get_sync_pipeline_status
                sync_status = get_sync_pipeline_status(self.config.shared_root)
                self.state.record_sync_pipeline(sync_status)
                if sync_status.get("inbox_files", 0) > 0:
                    logger.debug(
                        "Sync pipeline: %d inbox file(s) pending from %s",
                        sync_status["inbox_files"],
                        sync_status["inbox_peers"],
                    )
            except Exception as exc:
                logger.warning("Sync pipeline status check failed: %s", exc)

            self._stop_event.wait(timeout=self.config.health_interval)

    def _sync_loop(self) -> None:
        """Periodically push vault sync."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.config.sync_interval)
            if self._stop_event.is_set():
                break
            self._component_mgr.heartbeat("sync")
            if self._runtime and self._runtime.is_initialized:
                try:
                    from .pillars.sync import push_seed
                    name = self._runtime.manifest.name
                    result = push_seed(self.config.home, name, encrypt=True)
                    if result:
                        self.state.record_sync()
                        logger.info("Vault sync push completed: %s", result.name)
                except Exception as exc:
                    logger.error("Sync push error: %s", exc)
                    self.state.record_error(f"Sync: {exc}")

    def _housekeeping_loop(self) -> None:
        """Periodically prune stale ACKs, envelopes, and seeds (hourly)."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=3600)
            if self._stop_event.is_set():
                break
            self._component_mgr.heartbeat("housekeeping")
            try:
                from .housekeeping import run_housekeeping

                results = run_housekeeping(
                    skcapstone_home=self.config.shared_root,
                )
                summary = results.get("summary", {})
                deleted = summary.get("total_deleted", 0)
                freed_mb = summary.get("total_freed_mb", 0)
                if deleted > 0:
                    logger.info(
                        "Housekeeping: pruned %d files, freed %.1f MB",
                        deleted,
                        freed_mb,
                    )
            except Exception as exc:
                logger.error("Housekeeping error: %s", exc)
                self.state.record_error(f"Housekeeping: {exc}")

    def _process_messages(self, envelopes: list) -> None:
        """Handle received messages — delegates to consciousness loop.

        Args:
            envelopes: List of received MessageEnvelope objects.
        """
        for env in envelopes:
            msg_id = getattr(env, "message_id", None) or str(uuid.uuid4())
            try:
                content = env.payload.content or ""
                content_preview = content[:50]
                content_type = (
                    env.payload.content_type.value
                    if hasattr(env.payload.content_type, "value")
                    else str(env.payload.content_type)
                )
                sender = getattr(env, "sender", "unknown")
                self.state.add_inflight(msg_id, {
                    "message_id": msg_id,
                    "sender": sender,
                    "content": content,
                    "content_type": content_type,
                    "received_at": datetime.now(timezone.utc).isoformat(),
                })
                logger.info(
                    "Message from %s: %s [%s]",
                    sender,
                    content_preview,
                    content_type,
                )
                if self._consciousness and self._consciousness._config.enabled:
                    self._consciousness.process_envelope(env)
                # Activity bus: consciousness processed event
                _activity.push("consciousness.processed", {
                    "sender": sender,
                    "content_type": content_type,
                    "preview": content_preview,
                })
                # Stream the new message to any connected WebSocket clients
                self._ws_broadcast({
                    "type": "message",
                    "sender": sender,
                    "content": content,
                    "content_type": content_type,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                self._journal_incoming(sender, content_preview)
                self.state.remove_inflight(msg_id)
            except Exception as exc:
                self.state.remove_inflight(msg_id)
                logger.warning("Failed to process message from %s: %s", getattr(env, "sender", "?"), exc)
                self.state.record_error(f"Process message: {exc}")

    def _journal_incoming(self, sender: str, preview: str) -> None:
        """Auto-journal an incoming SKComm message and store a tagged memory.

        Writes a journal entry (title='From {sender}', moments=[preview]) and
        stores a short-term memory tagged 'skcomm-received'.  Both operations
        are best-effort: failures are logged at DEBUG level and never bubble up.
        """
        try:
            from skmemory.journal import Journal, JournalEntry
            entry = JournalEntry(
                title=f"From {sender}",
                moments=[preview] if preview else [],
            )
            Journal().write_entry(entry)
            logger.debug("Journal entry written for incoming message from %s", sender)
        except Exception as exc:
            logger.debug("Auto-journal write failed: %s", exc)

        try:
            self._store_skcomm_receipt(sender, preview)
            logger.debug("SKComm receipt stored for incoming message from %s", sender)
        except Exception as exc:
            logger.debug("SKComm receipt store failed: %s", exc)

    def _store_skcomm_receipt(self, sender: str, preview: str) -> None:
        """Write a skcomm receipt to the skcomm/received/ directory.

        These are transport bookkeeping, NOT persistent memories, so they
        go to ``~/.skcapstone/agents/{agent}/skcomm/received/`` instead of
        polluting the memory/ tree that skmemory indexes.
        """
        import json
        import uuid
        from datetime import datetime, timezone

        from . import active_agent_name

        agent_name = os.environ.get("SKCAPSTONE_AGENT") or active_agent_name()
        recv_dir = self.config.home / "agents" / agent_name / "skcomm" / "received"
        recv_dir.mkdir(parents=True, exist_ok=True)

        receipt_id = uuid.uuid4().hex[:12]
        receipt = {
            "id": receipt_id,
            "sender": sender,
            "preview": preview,
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        path = recv_dir / f"{receipt_id}.json"
        path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")

    def _healing_loop(self) -> None:
        """Periodically run self-healing diagnostics (every 5 min)."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=300)
            if self._stop_event.is_set():
                break
            self._component_mgr.heartbeat("healing")
            if self._healer:
                try:
                    report = self._healer.diagnose_and_heal()
                    self.state.record_healing_run(report)

                    checks_run = report.get("checks_run", 0)
                    auto_fixed = report.get("auto_fixed", 0)
                    still_broken = report.get("still_broken", 0)

                    if still_broken > 0:
                        logger.warning(
                            "Self-healing: %d checks, %d fixed, %d critical issue(s): %s",
                            checks_run,
                            auto_fixed,
                            still_broken,
                            report.get("escalated", []),
                        )
                    elif auto_fixed > 0:
                        logger.info(
                            "Self-healing: %d checks, %d fixed, all healthy",
                            checks_run,
                            auto_fixed,
                        )
                    else:
                        logger.debug("Self-healing: %d checks all ok", checks_run)
                except Exception as exc:
                    logger.error("Self-healing error: %s", exc)
                    self.state.record_error(f"Self-healing: {exc}")

            # Update consciousness stats
            if self._consciousness:
                self.state.consciousness_stats = self._consciousness.stats

    def _ws_broadcast(self, msg: dict) -> None:
        """Broadcast a JSON message to all connected WebSocket clients.

        Dead sockets are silently removed from the client set.

        Args:
            msg: JSON-serialisable dict to send as a text frame.
        """
        with self._ws_lock:
            if not self._ws_clients:
                return
            clients = set(self._ws_clients)
        frame = _ws_encode_frame(json.dumps(msg, default=str).encode("utf-8"))
        dead: set = set()
        for sock in clients:
            try:
                sock.sendall(frame)
            except OSError:
                dead.add(sock)
        if dead:
            with self._ws_lock:
                self._ws_clients -= dead

    def _start_api_server(self) -> None:
        """Start the local HTTP API server in a background thread."""
        from .rate_limiter import RateLimiter

        service = self
        state = self.state
        config = self.config
        consciousness = self._consciousness
        runtime = self._runtime
        rate_limiter = RateLimiter(requests_per_minute=100)

        class DaemonHandler(BaseHTTPRequestHandler):
            """HTTP handler for daemon status API."""

            @staticmethod
            def _hb_alive(hb: dict) -> bool:
                """Return True if heartbeat is within its TTL."""
                ts_str = hb.get("timestamp", "")
                ttl = hb.get("ttl_seconds", 300)
                if not ts_str:
                    return False
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    return datetime.now(timezone.utc) <= ts + timedelta(seconds=ttl)
                except Exception as e:
                    logger.warning("Failed to parse heartbeat timestamp %r: %s", ts_str, e)
                    return False

            @staticmethod
            def _get_system_stats() -> dict:
                """Collect memory and disk usage statistics."""
                import shutil
                stats: dict = {}
                try:
                    usage = shutil.disk_usage("/")
                    stats["disk_total_gb"] = round(usage.total / (1024 ** 3), 1)
                    stats["disk_used_gb"] = round(usage.used / (1024 ** 3), 1)
                    stats["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
                except Exception as e:
                    logger.warning("Failed to collect disk stats: %s", e)
                    stats.update(disk_total_gb=0, disk_used_gb=0, disk_free_gb=0)
                try:
                    import platform as _platform
                    if _platform.system() == "Linux":
                        meminfo: dict = {}
                        with open("/proc/meminfo") as fh:
                            for line in fh:
                                parts = line.split()
                                if len(parts) >= 2:
                                    meminfo[parts[0].rstrip(":")] = int(parts[1])
                        total_kb = meminfo.get("MemTotal", 0)
                        avail_kb = meminfo.get("MemAvailable", 0)
                        stats["memory_total_mb"] = round(total_kb / 1024)
                        stats["memory_used_mb"] = round((total_kb - avail_kb) / 1024)
                        stats["memory_free_mb"] = round(avail_kb / 1024)
                    else:
                        import psutil
                        mem = psutil.virtual_memory()
                        stats["memory_total_mb"] = round(mem.total / (1024 * 1024))
                        stats["memory_used_mb"] = round((mem.total - mem.available) / (1024 * 1024))
                        stats["memory_free_mb"] = round(mem.available / (1024 * 1024))
                except Exception as e:
                    logger.warning("Failed to collect memory stats: %s", e)
                    stats.update(memory_total_mb=0, memory_used_mb=0, memory_free_mb=0)
                return stats

            def _build_dashboard_data(self) -> dict:
                """Assemble all dashboard data into a single dict."""
                snap = state.snapshot()

                # Agent identity — try runtime first, then identity.json
                agent_name = "unknown"
                agent_fingerprint = ""
                if runtime and hasattr(runtime, "manifest"):
                    try:
                        agent_name = runtime.manifest.name or agent_name
                        agent_fingerprint = getattr(runtime.manifest, "fingerprint", "")
                    except Exception as exc:
                        logger.warning("Failed to read agent name from runtime manifest: %s", exc)
                identity_file = config.home / "identity" / "identity.json"
                if identity_file.exists():
                    try:
                        ident = json.loads(identity_file.read_text(encoding="utf-8"))
                        agent_name = ident.get("name", agent_name)
                        agent_fingerprint = ident.get("fingerprint", agent_fingerprint)
                    except Exception as exc:
                        logger.warning("Failed to read identity.json for dashboard: %s", exc)

                # Consciousness stats
                c_stats: dict = snap.get("consciousness", {})
                if consciousness:
                    c_stats = consciousness.stats

                # Recent conversations (last 5 by mtime)
                conversations: list = []
                conversations_dir = config.shared_root / "conversations"
                if conversations_dir.exists():
                    try:
                        conv_files = sorted(
                            conversations_dir.glob("*.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        )[:5]
                        for cf in conv_files:
                            try:
                                msgs = json.loads(cf.read_text(encoding="utf-8"))
                                if isinstance(msgs, list):
                                    conversations.append({
                                        "peer": cf.stem,
                                        "message_count": len(msgs),
                                        "last_message": msgs[-1].get("timestamp") if msgs else None,
                                    })
                            except Exception as exc:
                                logger.warning("Failed to read conversation file %s: %s", cf, exc)
                    except Exception as exc:
                        logger.warning("Failed to list conversation files: %s", exc)

                return {
                    "agent": {
                        "name": agent_name,
                        "fingerprint": agent_fingerprint,
                    },
                    "daemon": {
                        "running": snap["running"],
                        "uptime_seconds": snap["uptime_seconds"],
                        "pid": snap["pid"],
                        "messages_received": snap["messages_received"],
                        "syncs_completed": snap["syncs_completed"],
                    },
                    "consciousness": c_stats,
                    "backends": snap.get("transport_health", {}),
                    "conversations": conversations,
                    "system": self._get_system_stats(),
                    "recent_errors": snap.get("recent_errors", [])[-5:],
                }

            def _build_capstone_data(self) -> dict:
                """Assemble data for the GET /dashboard page.

                Returns pillar status, memory stats, coordination board
                summary + active tasks, and consciousness stats in one shot.
                """
                # ── Agent identity ────────────────────────────────────────
                agent: dict = {"name": "unknown", "fingerprint": "",
                               "consciousness": "AWAKENING",
                               "is_conscious": False, "is_singular": False}
                if runtime and hasattr(runtime, "manifest"):
                    try:
                        m = runtime.manifest
                        agent["name"] = m.name or agent["name"]
                        agent["fingerprint"] = getattr(m, "fingerprint", "") or ""
                        agent["is_conscious"] = bool(m.is_conscious)
                        agent["is_singular"] = bool(m.is_singular)
                        if m.is_singular:
                            agent["consciousness"] = "SINGULAR"
                        elif m.is_conscious:
                            agent["consciousness"] = "CONSCIOUS"
                    except Exception as exc:
                        logger.warning("Failed to read agent identity from runtime manifest: %s", exc)
                identity_file = config.home / "identity" / "identity.json"
                if identity_file.exists():
                    try:
                        ident = json.loads(identity_file.read_text(encoding="utf-8"))
                        agent["name"] = ident.get("name", agent["name"])
                        agent["fingerprint"] = ident.get("fingerprint", agent["fingerprint"])
                    except Exception as exc:
                        logger.warning("Failed to read identity.json for capstone dashboard: %s", exc)

                # ── Pillar status ─────────────────────────────────────────
                pillars: dict = {}
                if runtime and hasattr(runtime, "manifest"):
                    try:
                        pillars = {
                            k: v.value
                            for k, v in runtime.manifest.pillar_summary.items()
                        }
                    except Exception as exc:
                        logger.warning("Failed to read pillar summary from manifest: %s", exc)

                # ── Memory stats ──────────────────────────────────────────
                memory: dict = {}
                try:
                    from .memory_engine import get_stats as _mem_stats
                    ms = _mem_stats(config.home)
                    memory = {
                        "total": ms.total_memories,
                        "short_term": ms.short_term,
                        "mid_term": ms.mid_term,
                        "long_term": ms.long_term,
                        "status": ms.status.value,
                    }
                except Exception as exc:
                    logger.warning("Failed to collect memory stats for dashboard: %s", exc)

                # ── Coordination board ────────────────────────────────────
                board: dict = {"summary": {}, "active": []}
                try:
                    from .coordination import Board
                    brd = Board(config.home)
                    views = brd.get_task_views()
                    total = len(views)
                    done = sum(1 for v in views if v.status.value == "done")
                    in_prog = sum(1 for v in views if v.status.value == "in_progress")
                    claimed = sum(1 for v in views if v.status.value == "claimed")
                    open_ = sum(1 for v in views if v.status.value == "open")
                    active_tasks = [
                        {
                            "id": v.task.id,
                            "title": v.task.title,
                            "priority": v.task.priority.value,
                            "status": v.status.value,
                            "claimed_by": v.claimed_by,
                        }
                        for v in views
                        if v.status.value in ("in_progress", "claimed")
                    ]
                    board = {
                        "summary": {
                            "total": total,
                            "done": done,
                            "in_progress": in_prog,
                            "claimed": claimed,
                            "open": open_,
                        },
                        "active": active_tasks,
                    }
                except Exception as exc:
                    logger.warning("Failed to collect coordination board data for dashboard: %s", exc)

                # ── Consciousness stats ───────────────────────────────────
                c_stats: dict = {}
                if consciousness:
                    try:
                        c_stats = dict(consciousness.stats)
                    except Exception as exc:
                        logger.warning("Failed to read consciousness stats for dashboard: %s", exc)

                return {
                    "agent": agent,
                    "pillars": pillars,
                    "memory": memory,
                    "board": board,
                    "consciousness": c_stats,
                }

            @staticmethod
            def _render_html(data: dict) -> str:
                """Render dashboard data as a self-contained dark-theme HTML page."""
                agent = data.get("agent", {})
                d = data.get("daemon", {})
                cons = data.get("consciousness", {})
                backends = data.get("backends", {})
                conversations = data.get("conversations", [])
                system = data.get("system", {})
                errors = data.get("recent_errors", [])

                # Uptime formatting
                secs = float(d.get("uptime_seconds", 0))
                if secs < 60:
                    uptime_str = f"{int(secs)}s"
                elif secs < 3600:
                    uptime_str = f"{int(secs // 60)}m {int(secs % 60)}s"
                else:
                    uptime_str = f"{int(secs // 3600)}h {int((secs % 3600) // 60)}m"

                # Fingerprint — shorten for display
                fp = agent.get("fingerprint", "")
                fp_short = f"{fp[:8]}\u2026{fp[-8:]}" if len(fp) > 20 else fp

                # Consciousness card
                c_enabled = cons.get("enabled", False)
                c_dot = "dot-green" if c_enabled else "dot-red"
                c_inotify = cons.get("inotify_active", False)
                c_backends = cons.get("backends", [])
                c_backends_str = ", ".join(c_backends) if c_backends else "none"
                c_html = (
                    f'<div class="stat-row"><span class="stat-label">'
                    f'<span class="dot {c_dot}"></span>Status</span>'
                    f'<span class="stat-value">{"active" if c_enabled else "disabled"}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Processed</span>'
                    f'<span class="stat-value">{cons.get("messages_processed", 0)}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Responses sent</span>'
                    f'<span class="stat-value">{cons.get("responses_sent", 0)}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Errors</span>'
                    f'<span class="stat-value">{cons.get("errors", 0)}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">iNotify</span>'
                    f'<span class="stat-value">{"yes" if c_inotify else "no"}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">LLM backends</span>'
                    f'<span class="stat-value" style="font-size:12px">{c_backends_str}</span></div>'
                )

                # Backend health card
                if backends:
                    b_rows = []
                    for bname, binfo in backends.items():
                        avail = binfo.get("available", False) if isinstance(binfo, dict) else False
                        dot = "dot-green" if avail else "dot-red"
                        b_rows.append(
                            f'<div class="stat-row"><span class="stat-label">'
                            f'<span class="dot {dot}"></span>{bname}</span>'
                            f'<span class="stat-value">{"ok" if avail else "down"}</span></div>'
                        )
                    b_html = "\n".join(b_rows)
                else:
                    b_html = '<div style="color:#484f58;padding:4px 0;font-size:13px">No transports configured</div>'

                # Conversations card
                if conversations:
                    c_rows = []
                    for conv in conversations:
                        peer = conv.get("peer", "?")
                        count = conv.get("message_count", 0)
                        last = (conv.get("last_message") or "")[:10]
                        c_rows.append(
                            f'<div class="peer-row">'
                            f'<span class="peer-name">{peer}</span>'
                            f'<div><span class="peer-count">{count}</span>'
                            f'<span style="color:#484f58;font-size:11px;margin-left:6px">{last}</span>'
                            f'</div></div>'
                        )
                    conv_html = "\n".join(c_rows)
                else:
                    conv_html = '<div style="color:#484f58;padding:4px 0">No conversations yet</div>'

                # System stats card
                mem_used = system.get("memory_used_mb", 0)
                mem_total = system.get("memory_total_mb", 0)
                disk_free = system.get("disk_free_gb", 0)
                disk_total = system.get("disk_total_gb", 0)
                mem_pct = int(mem_used / mem_total * 100) if mem_total else 0
                disk_used_pct = int((disk_total - disk_free) / disk_total * 100) if disk_total else 0
                sys_html = (
                    f'<div class="stat-row"><span class="stat-label">RAM used</span>'
                    f'<span class="stat-value">{int(mem_used):,} / {int(mem_total):,} MB ({mem_pct}%)</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Disk used</span>'
                    f'<span class="stat-value">{disk_total - disk_free:.1f} / {disk_total:.1f} GB</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Disk free</span>'
                    f'<span class="stat-value">{disk_free:.1f} GB ({100 - disk_used_pct}%)</span></div>'
                )

                # Errors card
                if errors:
                    err_lines = "\n".join(
                        f'<div class="error-line">{str(e)[-100:]}</div>'
                        for e in errors[-5:]
                    )
                    err_html = f'<div class="error-list">{err_lines}</div>'
                else:
                    err_html = '<div style="color:#3fb950;font-size:13px">No recent errors</div>'

                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                agent_name = agent.get("name", "SKCapstone")
                pid = d.get("pid", "?")
                msg_count = d.get("messages_received", 0)
                syncs = d.get("syncs_completed", 0)

                # CSS stored as plain string to avoid f-string brace escaping
                css = (
                    "*{box-sizing:border-box;margin:0;padding:0}"
                    "body{background:#0d1117;color:#c9d1d9;font-family:'Segoe UI',system-ui,sans-serif;font-size:14px}"
                    "h1{font-size:20px;font-weight:600;color:#58a6ff}"
                    "h2{font-size:11px;font-weight:600;color:#8b949e;text-transform:uppercase;"
                    "letter-spacing:.08em;margin-bottom:10px}"
                    "header{padding:14px 24px;border-bottom:1px solid #21262d;"
                    "display:flex;align-items:center;gap:12px;flex-wrap:wrap}"
                    ".badge{font-size:11px;background:#161b22;border:1px solid #30363d;"
                    "border-radius:4px;padding:2px 8px;color:#8b949e}"
                    ".badge.ok{border-color:#238636;color:#3fb950}"
                    "main{padding:20px 24px;display:grid;"
                    "grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}"
                    ".card{background:#161b22;border:1px solid #21262d;border-radius:8px;padding:16px}"
                    ".stat-row{display:flex;justify-content:space-between;align-items:center;"
                    "padding:5px 0;border-bottom:1px solid #21262d}"
                    ".stat-row:last-child{border-bottom:none}"
                    ".stat-label{color:#8b949e;font-size:13px}"
                    ".stat-value{color:#e6edf3;font-family:monospace;font-size:13px;"
                    "text-align:right;max-width:55%}"
                    ".dot{display:inline-block;width:7px;height:7px;border-radius:50%;"
                    "margin-right:5px;vertical-align:middle}"
                    ".dot-green{background:#3fb950;box-shadow:0 0 4px #3fb95077}"
                    ".dot-red{background:#f85149;box-shadow:0 0 4px #f8514977}"
                    ".peer-row{display:flex;justify-content:space-between;align-items:center;"
                    "padding:6px 0;border-bottom:1px solid #21262d}"
                    ".peer-row:last-child{border-bottom:none}"
                    ".peer-name{color:#58a6ff;font-family:monospace;font-size:13px}"
                    ".peer-count{background:#1f6feb22;color:#79c0ff;border-radius:10px;"
                    "padding:1px 7px;font-size:12px}"
                    ".error-list{font-family:monospace;font-size:11px;color:#f85149}"
                    ".error-line{padding:2px 0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}"
                    "footer{padding:10px 24px;border-top:1px solid #21262d;"
                    "color:#484f58;font-size:11px;text-align:center}"
                )

                fp_badge = (
                    f'<span class="badge" style="font-size:10px;font-family:monospace">{fp_short}</span>'
                    if fp_short else ""
                )

                return (
                    f'<!DOCTYPE html><html lang="en"><head>'
                    f'<meta charset="UTF-8">'
                    f'<meta name="viewport" content="width=device-width,initial-scale=1.0">'
                    f'<title>SKCapstone \u2014 {agent_name}</title>'
                    f'<meta http-equiv="refresh" content="30">'
                    f'<style>{css}</style>'
                    f'</head><body>'
                    f'<header>'
                    f'<h1>&#9670; {agent_name}</h1>'
                    f'<span class="badge ok">DAEMON RUNNING</span>'
                    f'<span class="badge">PID {pid}</span>'
                    f'{fp_badge}'
                    f'<span style="margin-left:auto;color:#484f58;font-size:11px">auto-refresh 30s</span>'
                    f'</header>'
                    f'<main>'
                    f'<div class="card"><h2>Daemon</h2>'
                    f'<div class="stat-row"><span class="stat-label">Uptime</span>'
                    f'<span class="stat-value">{uptime_str}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Messages received</span>'
                    f'<span class="stat-value">{msg_count}</span></div>'
                    f'<div class="stat-row"><span class="stat-label">Syncs completed</span>'
                    f'<span class="stat-value">{syncs}</span></div>'
                    f'</div>'
                    f'<div class="card"><h2>Consciousness</h2>{c_html}</div>'
                    f'<div class="card"><h2>Backends</h2>{b_html}</div>'
                    f'<div class="card"><h2>Recent Conversations</h2>{conv_html}</div>'
                    f'<div class="card"><h2>System</h2>{sys_html}</div>'
                    f'<div class="card"><h2>Recent Errors</h2>{err_html}</div>'
                    f'</main>'
                    f'<footer>SKCapstone Daemon \u00b7 {ts}</footer>'
                    f'</body></html>'
                )

            def _check_rate_limit(self) -> bool:
                """Return True if the request is allowed; send 429 and return False otherwise."""
                ip = self.client_address[0]
                if not rate_limiter.is_allowed(ip):
                    self._json_response(
                        {"error": "rate limit exceeded", "retry_after_seconds": 60},
                        status=429,
                    )
                    return False
                return True

            def do_GET(self):
                """Handle GET requests to the daemon API."""
                if not self._check_rate_limit():
                    return
                if self.path == "/":
                    self._html_response(self._render_html(self._build_dashboard_data()))
                elif self.path == "/api/v1/dashboard":
                    self._json_response(self._build_dashboard_data())
                elif self.path == "/api/v1/health":
                    snap = state.snapshot()
                    healing = snap.get("self_healing", {})
                    sys_stats = self._get_system_stats()
                    c_enabled = False
                    if consciousness:
                        c_enabled = bool(consciousness.stats.get("enabled", False))
                    self._json_response({
                        "status": "ok" if snap["running"] else "stopped",
                        "uptime_seconds": snap["uptime_seconds"],
                        "daemon_pid": snap["pid"],
                        "consciousness_enabled": c_enabled,
                        "self_healing_last_run": healing.get("timestamp"),
                        "self_healing_issues_found": healing.get("still_broken", 0),
                        "self_healing_auto_fixed": healing.get("auto_fixed", 0),
                        "backend_health": snap.get("transport_health", {}),
                        "disk_free_gb": sys_stats.get("disk_free_gb", 0),
                        "memory_usage_mb": sys_stats.get("memory_used_mb", 0),
                    })
                elif self.path == "/status":
                    snap = state.snapshot()
                    snap["components"] = service._component_mgr.snapshot()
                    self._json_response(snap)
                elif self.path == "/api/v1/components":
                    self._json_response({"components": service._component_mgr.snapshot()})
                elif self.path == "/health":
                    self._json_response(state.health_reports)
                elif self.path == "/consciousness":
                    if consciousness:
                        self._json_response(consciousness.stats)
                    else:
                        self._json_response({"enabled": False, "reason": "not loaded"})
                elif self.path == "/ping":
                    self._json_response({"pong": True, "pid": os.getpid()})

                # ── Activity SSE stream ───────────────────────────────────
                elif self.path == "/api/v1/activity":
                    q: queue.Queue = queue.Queue(maxsize=200)
                    _activity.register_client(q)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self._add_cors_headers()
                    self.end_headers()
                    try:
                        # Replay history so late-joining clients see context
                        for chunk in _activity.get_history_encoded():
                            self.wfile.write(chunk)
                        self.wfile.flush()
                        # Stream live events; send keep-alive comments on timeout
                        while not service._stop_event.is_set():
                            try:
                                chunk = q.get(timeout=15)
                                self.wfile.write(chunk)
                                self.wfile.flush()
                            except queue.Empty:
                                self.wfile.write(b": heartbeat\n\n")
                                self.wfile.flush()
                    except OSError:
                        pass
                    finally:
                        _activity.unregister_client(q)
                    return

                # ── Vanilla-JS dashboard (single-file HTML) ───────────────
                elif self.path == "/dashboard":
                    html_file = Path(__file__).parent / "dashboard.html"
                    if html_file.exists():
                        self._html_response(html_file.read_text(encoding="utf-8"))
                    else:
                        self._html_response(
                            "<h1>dashboard.html not found</h1>", status=404
                        )

                # ── Capstone API (pillars + memory + board + consciousness) ─
                elif self.path == "/api/v1/capstone":
                    self._json_response(self._build_capstone_data())

                # ── WebSocket streaming endpoint ─────────────────────────
                elif self.path == "/ws":
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                        self._json_response(
                            {"error": "WebSocket upgrade required", "hint": "use ws://"},
                            status=400,
                        )
                        return
                    accept = _ws_accept_key(key)
                    # Flush any pending write-buffer data before raw-socket takeover
                    try:
                        self.wfile.flush()
                    except OSError:
                        return
                    # Send the 101 Switching Protocols response directly
                    try:
                        self.request.sendall((
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode("ascii"))
                    except OSError:
                        return
                    sock = self.request
                    with service._ws_lock:
                        service._ws_clients.add(sock)
                    # Send initial state snapshot
                    try:
                        init_payload = json.dumps(
                            {"type": "connected", "state": state.snapshot()},
                            default=str,
                        ).encode("utf-8")
                        sock.sendall(_ws_encode_frame(init_payload))
                    except OSError:
                        with service._ws_lock:
                            service._ws_clients.discard(sock)
                        return
                    # Read loop: handle close frames and detect disconnects
                    sock.settimeout(30)
                    try:
                        while not service._stop_event.is_set():
                            try:
                                result = _ws_read_frame(sock)
                            except TimeoutError:
                                continue  # check stop_event, then resume
                            except OSError:
                                break
                            if result is None:
                                break
                            opcode, _ = result
                            if opcode == 0x8:  # close frame
                                try:
                                    sock.sendall(_ws_encode_close())
                                except OSError:
                                    pass
                                break
                    finally:
                        with service._ws_lock:
                            service._ws_clients.discard(sock)

                # ── Log stream WebSocket endpoint (CapAuth required) ─────
                elif self.path == "/api/v1/logs":
                    key = self.headers.get("Sec-WebSocket-Key", "")
                    if self.headers.get("Upgrade", "").lower() != "websocket" or not key:
                        self._json_response(
                            {"error": "WebSocket upgrade required", "hint": "use ws://"},
                            status=400,
                        )
                        return

                    # Validate CapAuth bearer token before upgrading
                    auth_header = self.headers.get("Authorization", "")
                    token_str = auth_header[7:].strip() if auth_header.startswith("Bearer ") else None

                    fingerprint: Optional[str] = None
                    try:
                        from skcomm.capauth_validator import CapAuthValidator
                        fingerprint = CapAuthValidator(require_auth=True).validate(token_str)
                    except ImportError:
                        # skcomm not installed — fall back to skcapstone signed tokens
                        if token_str:
                            try:
                                from .tokens import import_token, verify_token
                                tok = import_token(token_str)
                                if verify_token(tok, home=config.home):
                                    fingerprint = tok.payload.issuer
                            except Exception as e:
                                logger.warning("Token verification fallback failed: %s", e)
                                fingerprint = None

                    if fingerprint is None:
                        self.send_response(401)
                        self.send_header("Content-Type", "application/json")
                        self._add_cors_headers()
                        self.end_headers()
                        self.wfile.write(b'{"error": "unauthorized"}')
                        return

                    # Perform WebSocket upgrade
                    accept = _ws_accept_key(key)
                    try:
                        self.wfile.flush()
                    except OSError:
                        return
                    try:
                        self.request.sendall((
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                        ).encode("ascii"))
                    except OSError:
                        return

                    sock = self.request
                    log_file = config.log_file
                    stop = service._stop_event

                    # Send the last 50 lines from daemon.log, record EOF offset
                    tail_offset: int = 0
                    try:
                        if log_file.exists():
                            from collections import deque as _deque
                            with open(log_file, encoding="utf-8", errors="replace") as _fh:
                                tail_lines = list(_deque(_fh, maxlen=50))
                                tail_offset = _fh.tell()
                            for _line in tail_lines:
                                _line = _line.rstrip("\n")
                                _frame = _ws_encode_frame(
                                    json.dumps(
                                        {"type": "line", "line": _line}, default=str
                                    ).encode("utf-8")
                                )
                                sock.sendall(_frame)
                    except OSError:
                        return

                    # Per-client tail thread: stream new log lines as they arrive
                    def _tail_and_send(
                        _sock=sock,
                        _log=log_file,
                        _stop=stop,
                        _offset=tail_offset,
                    ):
                        try:
                            # Wait for the log file if it doesn't exist yet
                            while not _stop.is_set() and not _log.exists():
                                _stop.wait(timeout=1.0)
                            if _stop.is_set():
                                return
                            with open(_log, encoding="utf-8", errors="replace") as _fh:
                                _fh.seek(_offset)
                                while not _stop.is_set():
                                    chunk = _fh.read()
                                    if chunk:
                                        for _ln in chunk.splitlines():
                                            _f = _ws_encode_frame(
                                                json.dumps(
                                                    {"type": "line", "line": _ln},
                                                    default=str,
                                                ).encode("utf-8")
                                            )
                                            try:
                                                _sock.sendall(_f)
                                            except OSError:
                                                return
                                    _stop.wait(timeout=0.5)
                        except OSError:
                            pass

                    threading.Thread(
                        target=_tail_and_send,
                        name="ws-logs-tail",
                        daemon=True,
                    ).start()

                    # Read loop: keep alive and detect client disconnect / close frame
                    sock.settimeout(30)
                    try:
                        while not service._stop_event.is_set():
                            try:
                                result = _ws_read_frame(sock)
                            except TimeoutError:
                                continue
                            except OSError:
                                break
                            if result is None:
                                break
                            opcode, _ = result
                            if opcode == 0x8:  # close frame
                                try:
                                    sock.sendall(_ws_encode_close())
                                except OSError:
                                    pass
                                break
                    finally:
                        pass  # tail thread is daemon — exits when socket closes

                # ── Household: list all agents ───────────────────────────
                elif self.path == "/api/v1/household/agents":
                    agents = []
                    agents_dir = config.shared_root / "agents"
                    heartbeats_dir = config.shared_root / "heartbeats"

                    if agents_dir.exists():
                        for agent_dir in sorted(agents_dir.iterdir()):
                            if not agent_dir.is_dir():
                                continue
                            agent_name = agent_dir.name
                            entry: dict = {"name": agent_name}

                            identity_path = agent_dir / "identity" / "identity.json"
                            if identity_path.exists():
                                try:
                                    entry["identity"] = json.loads(
                                        identity_path.read_text(encoding="utf-8")
                                    )
                                except Exception as exc:
                                    logger.warning("Failed to read identity for agent %s: %s", agent_name, exc)

                            hb_path = heartbeats_dir / f"{agent_name.lower()}.json"
                            if hb_path.exists():
                                try:
                                    hb = json.loads(hb_path.read_text(encoding="utf-8"))
                                    alive = self._hb_alive(hb)
                                    hb["alive"] = alive
                                    entry["heartbeat"] = hb
                                    entry["status"] = hb.get("status", "unknown") if alive else "stale"
                                except Exception as exc:
                                    logger.warning("Failed to read heartbeat for agent %s: %s", agent_name, exc)
                                    entry["status"] = "unknown"
                            else:
                                entry["status"] = "no_heartbeat"

                            if consciousness:
                                entry["consciousness"] = consciousness.stats

                            agents.append(entry)

                    self._json_response({"agents": agents})

                # ── Household: single agent detail ───────────────────────
                elif self.path.startswith("/api/v1/household/agent/"):
                    name = self.path[len("/api/v1/household/agent/"):].split("?")[0].rstrip("/")
                    if not name:
                        self._json_response({"error": "agent name required"}, status=400)
                        return

                    agent_dir = config.shared_root / "agents" / name
                    if not agent_dir.exists():
                        self._json_response({"error": f"agent '{name}' not found"}, status=404)
                        return

                    entry = {"name": name}

                    identity_path = agent_dir / "identity" / "identity.json"
                    if identity_path.exists():
                        try:
                            entry["identity"] = json.loads(
                                identity_path.read_text(encoding="utf-8")
                            )
                        except Exception as exc:
                            logger.warning("Failed to read identity for agent %s: %s", name, exc)

                    hb_path = config.shared_root / "heartbeats" / f"{name.lower()}.json"
                    if hb_path.exists():
                        try:
                            hb = json.loads(hb_path.read_text(encoding="utf-8"))
                            alive = self._hb_alive(hb)
                            hb["alive"] = alive
                            entry["heartbeat"] = hb
                            entry["status"] = hb.get("status", "unknown") if alive else "stale"
                        except Exception as exc:
                            logger.warning("Failed to read heartbeat for agent %s: %s", name, exc)

                    memory_dir = agent_dir / "memory"
                    if memory_dir.exists():
                        count = 0
                        for layer in ("short-term", "mid-term", "long-term"):
                            layer_dir = memory_dir / layer
                            if layer_dir.exists():
                                count += sum(1 for _ in layer_dir.glob("*.json"))
                        entry["memory_count"] = count

                    conversations_dir = config.shared_root / "conversations"
                    conv_list = []
                    if conversations_dir.exists():
                        for cf in sorted(conversations_dir.glob("*.json"))[:10]:
                            try:
                                msgs = json.loads(cf.read_text(encoding="utf-8"))
                                if isinstance(msgs, list):
                                    conv_list.append({
                                        "peer": cf.stem,
                                        "message_count": len(msgs),
                                        "last_message": msgs[-1].get("timestamp") if msgs else None,
                                    })
                            except Exception as exc:
                                logger.warning("Failed to read conversation file %s: %s", cf, exc)
                    entry["recent_conversations"] = conv_list

                    if consciousness:
                        entry["consciousness"] = consciousness.stats

                    self._json_response(entry)

                # ── Conversations: list all ───────────────────────────────
                elif self.path == "/api/v1/conversations":
                    conversations = []
                    conversations_dir = config.shared_root / "conversations"
                    if conversations_dir.exists():
                        for cf in sorted(
                            conversations_dir.glob("*.json"),
                            key=lambda p: p.stat().st_mtime,
                            reverse=True,
                        ):
                            try:
                                msgs = json.loads(cf.read_text(encoding="utf-8"))
                                if isinstance(msgs, list):
                                    last_msg = msgs[-1] if msgs else {}
                                    last_content = last_msg.get("content", last_msg.get("message", ""))
                                    conversations.append({
                                        "peer": cf.stem,
                                        "message_count": len(msgs),
                                        "last_message_time": last_msg.get("timestamp") if msgs else None,
                                        "last_message_preview": (last_content or "")[:120],
                                    })
                            except Exception as exc:
                                logger.warning("Failed to read conversation file %s: %s", cf, exc)
                    self._json_response({"conversations": conversations})

                # ── Conversations: single peer history ────────────────────
                elif self.path.startswith("/api/v1/conversations/"):
                    raw_peer = self.path[len("/api/v1/conversations/"):].split("?")[0].rstrip("/")
                    # Strip trailing /send so GET on .../peer (not /send) is unambiguous
                    if raw_peer.endswith("/send"):
                        self._json_response({"error": "use POST for /send"}, status=405)
                        return
                    peer = _sanitize_peer(raw_peer)
                    if not peer:
                        self._json_response({"error": "peer name required"}, status=400)
                        return

                    conv_file = config.shared_root / "conversations" / f"{peer}.json"
                    if not conv_file.exists():
                        self._json_response({"error": f"no conversation with '{peer}'"}, status=404)
                        return

                    try:
                        msgs = json.loads(conv_file.read_text(encoding="utf-8"))
                        self._json_response({"peer": peer, "messages": msgs})
                    except Exception as exc:
                        self._json_response({"error": str(exc)}, status=500)

                # ── Metrics: consciousness loop runtime stats ─────────────
                elif self.path == "/api/v1/metrics":
                    if consciousness:
                        self._json_response(consciousness.metrics.to_dict())
                    else:
                        self._json_response({"error": "consciousness not loaded"}, status=503)

                else:
                    self._json_response(
                        {
                            "endpoints": [
                                "/ (HTML dashboard)",
                                "/dashboard (vanilla-JS polling dashboard)",
                                "/api/v1/capstone (pillars + memory + board + consciousness)",
                                "/api/v1/dashboard",
                                "/api/v1/health",
                                "/status",
                                "/health",
                                "/consciousness",
                                "/ping",
                                "/api/v1/household/agents",
                                "/api/v1/household/agent/{name}",
                                "/api/v1/conversations",
                                "/api/v1/conversations/{peer}",
                                "POST /api/v1/conversations/{peer}/send",
                                "DELETE /api/v1/conversations/{peer}",
                                "/api/v1/components",
                                "/api/v1/activity (SSE activity stream)",
                                "/api/v1/metrics",
                                "/ws (WebSocket streaming)",
                                "/api/v1/logs (WebSocket log stream, CapAuth required)",
                            ]
                        },
                        status=200,
                    )

            def do_POST(self):
                """Handle POST requests — conversation send endpoint."""
                if not self._check_rate_limit():
                    return
                # ── POST /api/v1/conversations/{peer}/send ────────────────
                if self.path.startswith("/api/v1/conversations/") and self.path.endswith("/send"):
                    raw_peer = self.path[len("/api/v1/conversations/"):-len("/send")]
                    peer = _sanitize_peer(raw_peer)
                    if not peer:
                        self._json_response({"error": "invalid peer name"}, status=400)
                        return

                    # Read and parse JSON body
                    try:
                        length = int(self.headers.get("Content-Length", 0))
                        body = self.rfile.read(length) if length > 0 else b"{}"
                        data = json.loads(body)
                    except Exception as e:
                        logger.warning("Failed to parse request JSON body: %s", e)
                        self._json_response({"error": "invalid JSON body"}, status=400)
                        return

                    content = (data.get("content") or "").strip()
                    if not content:
                        self._json_response({"error": "content is required"}, status=400)
                        return

                    message_id = str(uuid.uuid4())
                    ts = datetime.now(timezone.utc).isoformat()

                    # Build SKComm envelope
                    envelope = {
                        "message_id": message_id,
                        "sender": "api",
                        "recipient": peer,
                        "timestamp": ts,
                        "payload": {
                            "content": content,
                            "content_type": "text",
                        },
                    }

                    # Write to SKComm outbox
                    try:
                        outbox = config.shared_root / "sync" / "comms" / "outbox"
                        outbox.mkdir(parents=True, exist_ok=True)
                        (outbox / f"{message_id}.skc.json").write_text(
                            json.dumps(envelope, indent=2), encoding="utf-8"
                        )
                    except Exception as exc:
                        logger.warning("Outbox write failed for %s: %s", peer, exc)

                    # Process through consciousness loop if available (generates response)
                    if consciousness and consciousness._config.enabled:
                        try:
                            from types import SimpleNamespace
                            fake_payload = SimpleNamespace(
                                content=content,
                                content_type=SimpleNamespace(value="text"),
                            )
                            fake_env = SimpleNamespace(sender=peer, payload=fake_payload)
                            threading.Thread(
                                target=consciousness.process_envelope,
                                args=(fake_env,),
                                daemon=True,
                            ).start()
                        except Exception as exc:
                            logger.debug("Consciousness process skipped: %s", exc)

                    self._json_response({"status": "sent", "message_id": message_id})
                    return

                self._json_response({"error": "not found"}, status=404)

            def do_DELETE(self):
                """Handle DELETE requests — clear conversation history."""
                if not self._check_rate_limit():
                    return
                # ── DELETE /api/v1/conversations/{peer} ──────────────────
                if self.path.startswith("/api/v1/conversations/"):
                    raw_peer = self.path[len("/api/v1/conversations/"):].split("?")[0].rstrip("/")
                    # Reject sub-paths like /send
                    if "/" in raw_peer:
                        self._json_response({"error": "invalid path"}, status=400)
                        return
                    peer = _sanitize_peer(raw_peer)
                    if not peer:
                        self._json_response({"error": "invalid peer name"}, status=400)
                        return

                    conv_file = config.shared_root / "conversations" / f"{peer}.json"
                    if not conv_file.exists():
                        self._json_response({"error": f"no conversation with '{peer}'"}, status=404)
                        return

                    try:
                        conv_file.unlink()
                        self._json_response({"status": "deleted", "peer": peer})
                    except Exception as exc:
                        self._json_response({"error": str(exc)}, status=500)
                    return

                self._json_response({"error": "not found"}, status=404)

            def do_OPTIONS(self):
                """Handle OPTIONS preflight requests for CORS."""
                self.send_response(204)
                self._add_cors_headers()
                self.end_headers()

            def _add_cors_headers(self):
                """Add CORS headers to allow Flutter web access."""
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

            def _json_response(self, data: dict, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self._add_cors_headers()
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2, default=str).encode())

            def _html_response(self, html: str, status: int = 200):
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._add_cors_headers()
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                logger.debug("API: %s", format % args)

        try:
            self._server = ThreadingHTTPServer(("127.0.0.1", config.port), DaemonHandler)

            if config.tls_enabled:
                from .tls import build_ssl_context, cert_fingerprint_sha256, ensure_tls_cert

                cert_path, key_path = ensure_tls_cert(config.tls_dir)
                ssl_ctx = build_ssl_context(cert_path, key_path)
                self._server.socket = ssl_ctx.wrap_socket(
                    self._server.socket, server_side=True
                )
                fingerprint = cert_fingerprint_sha256(cert_path)
                logger.info(
                    "TLS enabled — certificate: %s  fingerprint(SHA-256): %s",
                    cert_path,
                    fingerprint,
                )
                scheme = "https"
            else:
                scheme = "http"

            t = threading.Thread(
                target=self._server.serve_forever,
                name="daemon-api",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            logger.info("API server listening on %s://127.0.0.1:%d", scheme, config.port)
        except OSError as exc:
            logger.error("Failed to start API server: %s", exc)
            self.state.record_error(f"API server: {exc}")

    def _setup_logging(self) -> None:
        """Configure structured JSON file logging and console logging."""
        from .log_config import configure_logging

        configure_logging(self.config.log_file)

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Received signal %s — stopping", signal.Signals(signum).name)
        self._stop_event.set()

    def _save_shutdown_state(self) -> None:
        """Persist in-flight messages and metrics to disk on shutdown.

        Writes ``shutdown_state.json`` to the agent home directory so the
        next startup can detect and resume any messages that were mid-flight
        when the daemon was stopped.
        """
        state_path = self.config.home / SHUTDOWN_STATE_FILE
        inflight = self.state.get_inflight()
        data = {
            "shutdown_at": datetime.now(timezone.utc).isoformat(),
            "inflight_messages": inflight,
            "metrics": {
                "messages_received": self.state.messages_received,
                "syncs_completed": self.state.syncs_completed,
            },
        }
        try:
            self.config.home.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info(
                "Shutdown state saved — %d in-flight message(s) persisted",
                len(inflight),
            )
        except Exception as exc:
            logger.error("Failed to save shutdown state: %s", exc)

    def _load_startup_state(self) -> None:
        """Load persisted shutdown state on startup.

        If a ``shutdown_state.json`` file exists from a previous run, restores
        the cumulative metrics and re-queues any in-flight messages through the
        consciousness loop.  The state file is removed after successful load.
        """
        state_path = self.config.home / SHUTDOWN_STATE_FILE
        if not state_path.exists():
            return

        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Could not read shutdown state: %s", exc)
            return

        shutdown_at = data.get("shutdown_at", "unknown")
        metrics = data.get("metrics", {})
        with self.state._lock:
            self.state.messages_received += metrics.get("messages_received", 0)
            self.state.syncs_completed += metrics.get("syncs_completed", 0)

        inflight = data.get("inflight_messages", [])
        if inflight:
            logger.warning(
                "Resuming %d in-flight message(s) from previous shutdown at %s",
                len(inflight),
                shutdown_at,
            )
            self._resume_inflight_messages(inflight)
        else:
            logger.info("Startup state loaded — no in-flight messages to resume")

        try:
            state_path.unlink()
        except Exception as exc:
            logger.warning("Could not remove shutdown state file: %s", exc)

    def _resume_inflight_messages(self, inflight: list) -> None:
        """Re-queue in-flight messages from a previous run.

        Each message is reconstructed as a lightweight namespace envelope and
        dispatched to the consciousness loop.  If consciousness is not available
        the messages are logged as dropped so nothing is silently lost.

        Args:
            inflight: List of serialized message dicts from ``shutdown_state.json``.
        """
        if not (self._consciousness and self._consciousness._config.enabled):
            logger.warning(
                "Consciousness not available — dropping %d in-flight message(s)",
                len(inflight),
            )
            for msg in inflight:
                logger.warning(
                    "  dropped: %s from %s",
                    msg.get("message_id"),
                    msg.get("sender"),
                )
            return

        from types import SimpleNamespace

        for msg in inflight:
            try:
                fake_payload = SimpleNamespace(
                    content=msg.get("content", ""),
                    content_type=SimpleNamespace(value=msg.get("content_type", "text")),
                )
                fake_env = SimpleNamespace(
                    message_id=msg.get("message_id", str(uuid.uuid4())),
                    sender=msg.get("sender", "unknown"),
                    payload=fake_payload,
                )
                self._consciousness.process_envelope(fake_env)
                logger.info(
                    "Resumed in-flight message %s from %s",
                    msg.get("message_id"),
                    msg.get("sender"),
                )
            except Exception as exc:
                logger.error(
                    "Failed to resume message %s: %s",
                    msg.get("message_id"),
                    exc,
                )

    def _write_pid(self) -> None:
        """Write the PID file."""
        pid_path = self.config.home / PID_FILE
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _remove_pid(self) -> None:
        """Remove the PID file."""
        pid_path = self.config.home / PID_FILE
        if pid_path.exists():
            pid_path.unlink()


def read_pid(home: Optional[Path] = None) -> Optional[int]:
    """Read the daemon PID from the PID file.

    Checks the given home directory first, then falls back to the shared
    root (AGENT_HOME / ~/.skcapstone) since the daemon writes its PID
    to config.home which defaults to the shared root.

    Args:
        home: Agent home directory (or shared root).

    Returns:
        PID as int, or None if not running.
    """
    home = (home or Path(AGENT_HOME)).expanduser()
    shared_root = Path(AGENT_HOME).expanduser()

    # Check agent home first, then shared root
    for candidate in (home, shared_root):
        pid_path = candidate / PID_FILE
        if not pid_path.exists():
            continue
        try:
            pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            pid_path.unlink(missing_ok=True)
    return None


def is_running(home: Optional[Path] = None) -> bool:
    """Check if the daemon is currently running.

    Args:
        home: Agent home directory.

    Returns:
        True if daemon process is alive.
    """
    return read_pid(home) is not None


def get_daemon_status(home: Optional[Path] = None, port: int = DEFAULT_PORT) -> Optional[dict]:
    """Query the running daemon's status via HTTP API.

    Args:
        home: Agent home directory.
        port: API port to query.

    Returns:
        Status dict from the daemon, or None if unreachable.
    """
    import urllib.request
    import urllib.error

    try:
        url = f"http://127.0.0.1:{port}/status"
        with urllib.request.urlopen(url, timeout=3) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
