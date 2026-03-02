"""
SKCapstone Daemon — the always-on sovereign agent.

Runs as a background process, continuously polling for
incoming messages, scheduling vault sync, monitoring
transport health, and exposing a local HTTP API for
connectors to query agent state.

This is what turns a CLI tool into a living agent.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional

from . import AGENT_HOME, SHARED_ROOT

logger = logging.getLogger("skcapstone.daemon")

DEFAULT_PORT = 7777
PID_FILE = "daemon.pid"
LOG_DIR = "logs"


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
    ):
        self.home = (home or Path(AGENT_HOME)).expanduser()
        self.shared_root = (shared_root or Path(SHARED_ROOT)).expanduser()
        self.poll_interval = poll_interval
        self.sync_interval = sync_interval
        self.health_interval = health_interval
        self.port = port
        self.consciousness_enabled = consciousness_enabled
        self.consciousness_config_path = consciousness_config_path

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
                "recent_errors": self.errors[-10:],
                "pid": os.getpid(),
            }

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

        self._load_components()

        workers = [
            ("poll", self._poll_loop),
            ("health", self._health_loop),
            ("sync", self._sync_loop),
            ("housekeeping", self._housekeeping_loop),
        ]
        for name, target in workers:
            t = threading.Thread(target=target, name=f"daemon-{name}", daemon=True)
            t.start()
            self._threads.append(t)

        # Start consciousness loop threads
        if self._consciousness:
            consciousness_threads = self._consciousness.start()
            self._threads.extend(consciousness_threads)

        # Start self-healing loop
        if self._healer:
            t = threading.Thread(
                target=self._healing_loop,
                name="daemon-healing",
                daemon=True,
            )
            t.start()
            self._threads.append(t)

        self._start_api_server()

        logger.info("Daemon started — PID %d", os.getpid())

    def stop(self) -> None:
        """Gracefully stop the daemon and all workers."""
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

    def _load_components(self) -> None:
        """Attempt to load SKComm, AgentRuntime, and ConsciousnessLoop."""
        try:
            from skcomm.core import SKComm
            self._skcomm = SKComm.from_config()
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

    def _poll_loop(self) -> None:
        """Continuously poll SKComm inbox for new messages."""
        while not self._stop_event.is_set():
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
        """Periodically check transport health."""
        while not self._stop_event.is_set():
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
                    self._beacon.pulse(consciousness_active=bool(self._consciousness))
                except Exception as exc:
                    logger.warning("Heartbeat pulse failed: %s", exc)

            self._stop_event.wait(timeout=self.config.health_interval)

    def _sync_loop(self) -> None:
        """Periodically push vault sync."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self.config.sync_interval)
            if self._stop_event.is_set():
                break

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
            try:
                content_preview = (env.payload.content or "")[:50]
                logger.info(
                    "Message from %s: %s [%s]",
                    env.sender,
                    content_preview,
                    env.payload.content_type.value,
                )
                if self._consciousness and self._consciousness._config.enabled:
                    self._consciousness.process_envelope(env)
            except Exception as exc:
                logger.warning("Failed to process message from %s: %s", getattr(env, "sender", "?"), exc)
                self.state.record_error(f"Process message: {exc}")

    def _healing_loop(self) -> None:
        """Periodically run self-healing diagnostics (every 5 min)."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=300)
            if self._stop_event.is_set():
                break

            if self._healer:
                try:
                    report = self._healer.diagnose_and_heal()
                    self.state.self_healing_report = report
                    if report.get("auto_fixed", 0) > 0:
                        logger.info(
                            "Self-healing: %d checks, %d fixed, %d broken",
                            report.get("checks_run", 0),
                            report.get("auto_fixed", 0),
                            report.get("still_broken", 0),
                        )
                except Exception as exc:
                    logger.error("Self-healing error: %s", exc)
                    self.state.record_error(f"Self-healing: {exc}")

            # Update consciousness stats
            if self._consciousness:
                self.state.consciousness_stats = self._consciousness.stats

    def _start_api_server(self) -> None:
        """Start the local HTTP API server in a background thread."""
        state = self.state
        config = self.config
        consciousness = self._consciousness

        class DaemonHandler(BaseHTTPRequestHandler):
            """HTTP handler for daemon status API."""

            def do_GET(self):
                """Handle GET requests to the daemon API."""
                if self.path == "/status":
                    self._json_response(state.snapshot())
                elif self.path == "/health":
                    self._json_response(state.health_reports)
                elif self.path == "/consciousness":
                    if consciousness:
                        self._json_response(consciousness.stats)
                    else:
                        self._json_response({"enabled": False, "reason": "not loaded"})
                elif self.path == "/ping":
                    self._json_response({"pong": True, "pid": os.getpid()})
                elif self.path == "/api/v1/household/agents":
                    agents = []
                    heartbeats_dir = config.shared_root / "heartbeats"
                    if heartbeats_dir.exists():
                        for hf in sorted(heartbeats_dir.glob("*.json")):
                            try:
                                data = json.loads(hf.read_text())
                                agents.append(data)
                            except Exception:
                                pass
                    self._json_response({"agents": agents})
                else:
                    self._json_response(
                        {
                            "endpoints": [
                                "/status",
                                "/health",
                                "/consciousness",
                                "/ping",
                                "/api/v1/household/agents",
                            ]
                        },
                        status=200,
                    )

            def _json_response(self, data: dict, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2, default=str).encode())

            def log_message(self, format, *args):
                logger.debug("API: %s", format % args)

        try:
            self._server = HTTPServer(("127.0.0.1", config.port), DaemonHandler)
            t = threading.Thread(
                target=self._server.serve_forever,
                name="daemon-api",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
            logger.info("API server listening on http://127.0.0.1:%d", config.port)
        except OSError as exc:
            logger.error("Failed to start API server: %s", exc)
            self.state.record_error(f"API server: {exc}")

    def _setup_logging(self) -> None:
        """Configure file and console logging."""
        handler = logging.FileHandler(self.config.log_file)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)

    def _setup_signals(self) -> None:
        """Register signal handlers for graceful shutdown."""
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, self._handle_signal)

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals."""
        logger.info("Received signal %s — stopping", signal.Signals(signum).name)
        self._stop_event.set()

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

    Args:
        home: Agent home directory.

    Returns:
        PID as int, or None if not running.
    """
    home = (home or Path(AGENT_HOME)).expanduser()
    pid_path = home / PID_FILE
    if not pid_path.exists():
        return None
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
