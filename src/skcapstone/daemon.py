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
from datetime import datetime, timedelta, timezone
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
        runtime = self._runtime

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
                except Exception:
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
                except Exception:
                    stats.update(disk_total_gb=0, disk_used_gb=0, disk_free_gb=0)
                try:
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
                except Exception:
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
                    except Exception:
                        pass
                identity_file = config.home / "identity" / "identity.json"
                if identity_file.exists():
                    try:
                        ident = json.loads(identity_file.read_text(encoding="utf-8"))
                        agent_name = ident.get("name", agent_name)
                        agent_fingerprint = ident.get("fingerprint", agent_fingerprint)
                    except Exception:
                        pass

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
                            except Exception:
                                pass
                    except Exception:
                        pass

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

            def do_GET(self):
                """Handle GET requests to the daemon API."""
                if self.path == "/":
                    self._html_response(self._render_html(self._build_dashboard_data()))
                elif self.path == "/api/v1/dashboard":
                    self._json_response(self._build_dashboard_data())
                elif self.path == "/status":
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
                                except Exception:
                                    pass

                            hb_path = heartbeats_dir / f"{agent_name.lower()}.json"
                            if hb_path.exists():
                                try:
                                    hb = json.loads(hb_path.read_text(encoding="utf-8"))
                                    alive = self._hb_alive(hb)
                                    hb["alive"] = alive
                                    entry["heartbeat"] = hb
                                    entry["status"] = hb.get("status", "unknown") if alive else "stale"
                                except Exception:
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
                        except Exception:
                            pass

                    hb_path = config.shared_root / "heartbeats" / f"{name.lower()}.json"
                    if hb_path.exists():
                        try:
                            hb = json.loads(hb_path.read_text(encoding="utf-8"))
                            alive = self._hb_alive(hb)
                            hb["alive"] = alive
                            entry["heartbeat"] = hb
                            entry["status"] = hb.get("status", "unknown") if alive else "stale"
                        except Exception:
                            pass

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
                            except Exception:
                                pass
                    entry["recent_conversations"] = conv_list

                    if consciousness:
                        entry["consciousness"] = consciousness.stats

                    self._json_response(entry)

                # ── Conversations: list all ───────────────────────────────
                elif self.path == "/api/v1/conversations":
                    conversations = []
                    conversations_dir = config.shared_root / "conversations"
                    if conversations_dir.exists():
                        for cf in sorted(conversations_dir.glob("*.json")):
                            try:
                                msgs = json.loads(cf.read_text(encoding="utf-8"))
                                if isinstance(msgs, list):
                                    conversations.append({
                                        "peer": cf.stem,
                                        "message_count": len(msgs),
                                        "last_message": msgs[-1].get("timestamp") if msgs else None,
                                    })
                            except Exception:
                                pass
                    self._json_response({"conversations": conversations})

                # ── Conversations: single peer history ────────────────────
                elif self.path.startswith("/api/v1/conversations/"):
                    peer = self.path[len("/api/v1/conversations/"):].split("?")[0].rstrip("/")
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
                                "/api/v1/dashboard",
                                "/status",
                                "/health",
                                "/consciousness",
                                "/ping",
                                "/api/v1/household/agents",
                                "/api/v1/household/agent/{name}",
                                "/api/v1/conversations",
                                "/api/v1/conversations/{peer}",
                                "/api/v1/metrics",
                            ]
                        },
                        status=200,
                    )

            def _json_response(self, data: dict, status: int = 200):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data, indent=2, default=str).encode())

            def _html_response(self, html: str, status: int = 200):
                body = html.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

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
