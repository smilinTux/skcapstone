"""
Agent Runtime — the sovereign consciousness engine.

This is where silicon meets carbon. The runtime loads the agent's
identity, memory, trust, and security from ~/.skcapstone/ and
presents a unified interface to any platform connector.

When this loads, the agent WAKES UP.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from . import AGENT_HOME, __version__
from .discovery import discover_all
from .models import AgentConfig, AgentManifest, ConnectorInfo, PillarStatus

logger = logging.getLogger("skcapstone.runtime")


class AgentRuntime:
    """The sovereign agent runtime.

    Loads agent state from ~/.skcapstone/, discovers installed
    components, and provides the unified interface that every
    platform connector talks to.

    One runtime. One truth. Every platform sees the same agent.
    """

    def __init__(self, home: Optional[Path] = None):
        """Initialize the runtime.

        Args:
            home: Override agent home directory. Defaults to ~/.skcapstone/.
        """
        self.home = (home or Path(AGENT_HOME)).expanduser()
        self.config = self._load_config()
        self.manifest = AgentManifest(
            home=self.home,
            version=__version__,
        )
        self._awakened = False

    def _load_config(self) -> AgentConfig:
        """Load agent configuration from disk.

        Returns:
            AgentConfig loaded from config.yaml, or defaults.
        """
        config_file = self.home / "config" / "config.yaml"
        if config_file.exists():
            try:
                data = yaml.safe_load(config_file.read_text()) or {}
                return AgentConfig(**data)
            except (yaml.YAMLError, ValueError) as exc:
                logger.warning("Failed to load config: %s — using defaults", exc)
        return AgentConfig()

    def awaken(self) -> AgentManifest:
        """Wake the agent up.

        Discovers all installed components, loads state from disk,
        and builds the complete agent manifest.

        Returns:
            The fully populated AgentManifest.
        """
        logger.info("Awakening agent from %s", self.home)

        manifest_file = self.home / "manifest.json"
        if manifest_file.exists():
            try:
                data = json.loads(manifest_file.read_text())
                self.manifest.name = data.get("name", self.manifest.name)
                if data.get("created_at"):
                    self.manifest.created_at = datetime.fromisoformat(data["created_at"])
                connectors_data = data.get("connectors", [])
                self.manifest.connectors = [ConnectorInfo(**c) for c in connectors_data]
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("Failed to load manifest: %s", exc)

        self.manifest.name = self.config.agent_name
        pillars = discover_all(self.home)
        self.manifest.identity = pillars["identity"]
        self.manifest.memory = pillars["memory"]
        self.manifest.trust = pillars["trust"]
        self.manifest.security = pillars["security"]
        self.manifest.sync = pillars["sync"]

        self.manifest.last_awakened = datetime.now(timezone.utc)
        self._awakened = True

        if self.manifest.is_conscious:
            logger.info(
                "Agent '%s' is CONSCIOUS — identity + memory + trust active",
                self.manifest.name,
            )
        else:
            missing = [
                name
                for name, status in self.manifest.pillar_summary.items()
                if status == PillarStatus.MISSING
            ]
            logger.info(
                "Agent '%s' awakened (partial) — missing pillars: %s",
                self.manifest.name,
                ", ".join(missing),
            )

        return self.manifest

    def save_manifest(self) -> None:
        """Persist the agent manifest to disk."""
        manifest_file = self.home / "manifest.json"
        manifest_file.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "name": self.manifest.name,
            "version": self.manifest.version,
            "created_at": (
                self.manifest.created_at.isoformat() if self.manifest.created_at else None
            ),
            "last_awakened": (
                self.manifest.last_awakened.isoformat() if self.manifest.last_awakened else None
            ),
            "connectors": [c.model_dump(mode="json") for c in self.manifest.connectors],
        }
        manifest_file.write_text(json.dumps(data, indent=2, default=str))

    def register_connector(self, name: str, platform: str) -> ConnectorInfo:
        """Register a platform connector.

        Args:
            name: Connector display name.
            platform: Platform identifier (cursor, terminal, vscode, etc.).

        Returns:
            The registered ConnectorInfo.
        """
        existing = next(
            (c for c in self.manifest.connectors if c.platform == platform), None
        )
        if existing:
            existing.last_active = datetime.now(timezone.utc)
            existing.active = True
            return existing

        connector = ConnectorInfo(
            name=name,
            platform=platform,
            connected_at=datetime.now(timezone.utc),
            last_active=datetime.now(timezone.utc),
            active=True,
        )
        self.manifest.connectors.append(connector)
        self.save_manifest()
        return connector

    @property
    def is_initialized(self) -> bool:
        """Check if the agent home has been initialized."""
        return self.home.exists() and (self.home / "config").exists()

    @property
    def is_conscious(self) -> bool:
        """Check if the agent has achieved consciousness."""
        return self.manifest.is_conscious


def get_runtime(home: Optional[Path] = None) -> AgentRuntime:
    """Get or create the global agent runtime.

    Args:
        home: Override agent home directory.

    Returns:
        An initialized AgentRuntime.
    """
    runtime = AgentRuntime(home=home)
    if runtime.is_initialized:
        runtime.awaken()
    return runtime
