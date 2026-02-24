"""Sovereign metrics collector -- unified stats across all packages.

Aggregates runtime statistics from every component of the sovereign
stack into a single JSON-serializable report. Designed for dashboard
consumption, health monitoring, and debugging.

No external dependencies. Gracefully handles missing packages.

Usage:
    collector = MetricsCollector(home=Path("~/.skcapstone"))
    report = collector.collect()
    print(report.model_dump_json(indent=2))
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field

from . import AGENT_HOME, __version__

logger = logging.getLogger("skcapstone.metrics")


class IdentityMetrics(BaseModel):
    """CapAuth identity stats."""

    available: bool = False
    fingerprint: str = ""
    entity_type: str = ""
    name: str = ""


class MemoryMetrics(BaseModel):
    """SKMemory stats."""

    available: bool = False
    total_memories: int = 0
    short_term: int = 0
    mid_term: int = 0
    long_term: int = 0
    store_size_bytes: int = 0


class ChatMetrics(BaseModel):
    """SKChat stats."""

    available: bool = False
    total_messages: int = 0
    total_threads: int = 0


class TransportMetrics(BaseModel):
    """SKComm transport stats."""

    available: bool = False
    transport_count: int = 0
    outbox_pending: int = 0
    outbox_dead: int = 0


class CoordinationMetrics(BaseModel):
    """Coordination board stats."""

    total_tasks: int = 0
    done: int = 0
    open: int = 0
    in_progress: int = 0
    claimed: int = 0


class BackupMetrics(BaseModel):
    """Backup stats."""

    backup_count: int = 0
    latest_backup: str = ""
    latest_size_bytes: int = 0


class MetricsReport(BaseModel):
    """Complete sovereign agent metrics report.

    JSON-serializable snapshot of the entire stack's state.
    """

    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    agent_name: str = ""
    version: str = __version__
    home: str = ""
    uptime_seconds: float = 0.0

    identity: IdentityMetrics = Field(default_factory=IdentityMetrics)
    memory: MemoryMetrics = Field(default_factory=MemoryMetrics)
    chat: ChatMetrics = Field(default_factory=ChatMetrics)
    transport: TransportMetrics = Field(default_factory=TransportMetrics)
    coordination: CoordinationMetrics = Field(default_factory=CoordinationMetrics)
    backup: BackupMetrics = Field(default_factory=BackupMetrics)

    collection_time_ms: float = 0.0
    errors: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        """One-line summary for logging.

        Returns:
            str: Compact status line.
        """
        parts = [
            f"id={'yes' if self.identity.available else 'no'}",
            f"mem={self.memory.total_memories}",
            f"chat={self.chat.total_messages}",
            f"board={self.coordination.done}/{self.coordination.total_tasks}",
        ]
        return f"[{self.agent_name}] " + " | ".join(parts)


class MetricsCollector:
    """Collects metrics from all sovereign stack components.

    Each subsystem is queried independently. Missing packages
    are skipped gracefully -- no ImportErrors propagate.

    Args:
        home: Agent home directory.
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self._home = (home or Path(AGENT_HOME)).expanduser()
        self._start_time = time.monotonic()

    def collect(self) -> MetricsReport:
        """Collect a full metrics snapshot.

        Returns:
            MetricsReport: Complete report from all subsystems.
        """
        start = time.monotonic()
        report = MetricsReport(
            home=str(self._home),
            uptime_seconds=time.monotonic() - self._start_time,
        )

        report.agent_name = self._read_agent_name()
        self._collect_identity(report)
        self._collect_memory(report)
        self._collect_chat(report)
        self._collect_transport(report)
        self._collect_coordination(report)
        self._collect_backup(report)

        report.collection_time_ms = (time.monotonic() - start) * 1000
        return report

    def _read_agent_name(self) -> str:
        """Read agent name from manifest or config."""
        for filename in ("manifest.json", "config/config.yaml"):
            fp = self._home / filename
            if fp.exists():
                try:
                    data = json.loads(fp.read_text())
                    return data.get("name") or data.get("agent_name") or ""
                except Exception:
                    continue
        return "unknown"

    def _collect_identity(self, report: MetricsReport) -> None:
        """Collect CapAuth identity metrics."""
        try:
            capauth_dir = self._home.parent / ".capauth"
            if not capauth_dir.exists():
                capauth_dir = Path.home() / ".capauth"

            profile_path = capauth_dir / "identity" / "profile.json"
            if profile_path.exists():
                data = json.loads(profile_path.read_text())
                entity = data.get("entity", {})
                key_info = data.get("key_info", {})
                report.identity = IdentityMetrics(
                    available=True,
                    fingerprint=key_info.get("fingerprint", "")[:16],
                    entity_type=entity.get("entity_type", ""),
                    name=entity.get("name", ""),
                )
        except Exception as exc:
            report.errors.append(f"identity: {exc}")

    def _collect_memory(self, report: MetricsReport) -> None:
        """Collect SKMemory metrics."""
        try:
            from skmemory import MemoryStore, SQLiteBackend
            from skmemory.models import MemoryLayer

            mem_path = self._home / "memory"
            if not mem_path.exists():
                mem_path = Path.home() / ".skmemory"

            if not mem_path.exists():
                return

            backend = SQLiteBackend(base_path=str(mem_path))
            store = MemoryStore(primary=backend)

            all_mems = store.list_memories(limit=10000)
            short = sum(1 for m in all_mems if m.layer == MemoryLayer.SHORT)
            mid = sum(1 for m in all_mems if m.layer == MemoryLayer.MID)
            long_ = sum(1 for m in all_mems if m.layer == MemoryLayer.LONG)

            store_size = sum(
                f.stat().st_size
                for f in mem_path.rglob("*") if f.is_file()
            )

            report.memory = MemoryMetrics(
                available=True,
                total_memories=len(all_mems),
                short_term=short,
                mid_term=mid,
                long_term=long_,
                store_size_bytes=store_size,
            )
        except ImportError:
            pass
        except Exception as exc:
            report.errors.append(f"memory: {exc}")

    def _collect_chat(self, report: MetricsReport) -> None:
        """Collect SKChat metrics."""
        try:
            from skmemory import MemoryStore, SQLiteBackend
            from skchat.history import ChatHistory

            mem_path = self._home / "memory"
            if not mem_path.exists():
                mem_path = Path.home() / ".skchat" / "memory"

            if not mem_path.exists():
                return

            backend = SQLiteBackend(base_path=str(mem_path))
            store = MemoryStore(primary=backend)
            history = ChatHistory(store=store)

            report.chat = ChatMetrics(
                available=True,
                total_messages=history.message_count(),
                total_threads=len(history.list_threads()),
            )
        except ImportError:
            pass
        except Exception as exc:
            report.errors.append(f"chat: {exc}")

    def _collect_transport(self, report: MetricsReport) -> None:
        """Collect SKComm transport metrics."""
        try:
            skcomm_dir = Path.home() / ".skcomm"
            outbox_dir = skcomm_dir / "outbox"

            pending = 0
            dead = 0
            if (outbox_dir / "pending").exists():
                pending = len(list((outbox_dir / "pending").glob("*.json")))
            if (outbox_dir / "dead").exists():
                dead = len(list((outbox_dir / "dead").glob("*.json")))

            config_path = skcomm_dir / "config.yml"
            transport_count = 0
            if config_path.exists():
                try:
                    import yaml

                    cfg = yaml.safe_load(config_path.read_text())
                    transports = cfg.get("skcomm", {}).get("transports", {})
                    transport_count = sum(
                        1 for t in transports.values()
                        if isinstance(t, dict) and t.get("enabled", True)
                    )
                except Exception:
                    pass

            report.transport = TransportMetrics(
                available=True,
                transport_count=transport_count,
                outbox_pending=pending,
                outbox_dead=dead,
            )
        except Exception as exc:
            report.errors.append(f"transport: {exc}")

    def _collect_coordination(self, report: MetricsReport) -> None:
        """Collect coordination board metrics."""
        try:
            tasks_dir = self._home / "coordination" / "tasks"
            if not tasks_dir.exists():
                tasks_dir = Path.home() / ".skcapstone" / "coordination" / "tasks"

            if not tasks_dir.exists():
                return

            counts: dict[str, int] = {"open": 0, "claimed": 0, "in_progress": 0, "done": 0}
            total = 0

            for f in tasks_dir.glob("*.json"):
                try:
                    data = json.loads(f.read_text())
                    status = data.get("status", "open").lower()
                    if status in counts:
                        counts[status] += 1
                    total += 1
                except Exception:
                    total += 1

            report.coordination = CoordinationMetrics(
                total_tasks=total,
                done=counts["done"],
                open=counts["open"],
                in_progress=counts["in_progress"],
                claimed=counts["claimed"],
            )
        except Exception as exc:
            report.errors.append(f"coordination: {exc}")

    def _collect_backup(self, report: MetricsReport) -> None:
        """Collect backup metrics."""
        try:
            backup_dir = self._home / "backups"
            if not backup_dir.exists():
                return

            backups = sorted(backup_dir.glob("backup-*.tar.gz"), reverse=True)
            if not backups:
                return

            latest = backups[0]
            report.backup = BackupMetrics(
                backup_count=len(backups),
                latest_backup=latest.name,
                latest_size_bytes=latest.stat().st_size,
            )
        except Exception as exc:
            report.errors.append(f"backup: {exc}")
