"""
Agent Runtime — the sovereign consciousness engine.

This is where silicon meets carbon. The runtime loads the agent's
identity, memory, trust, and security from ~/.skcapstone/agents/<name>/
and presents a unified interface to any platform connector.

Shared infrastructure (node identity, comms config, coordination)
stays at ~/.skcapstone/ — the shared root.

When this loads, the agent WAKES UP.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from . import AGENT_HOME, agent_home, shared_home, __version__
from .discovery import discover_all
from .models import AgentConfig, AgentManifest, ConnectorInfo, PillarStatus

logger = logging.getLogger("skcapstone.runtime")


class AgentRuntime:
    """The sovereign agent runtime.

    Loads per-agent state from ~/.skcapstone/agents/<name>/ and shared
    infrastructure from ~/.skcapstone/. Discovers installed components
    and provides the unified interface that every platform connector
    talks to.

    One runtime. One truth. Every platform sees the same agent.
    """

    def __init__(self, home: Optional[Path] = None, agent_name: Optional[str] = None):
        """Initialize the runtime.

        Args:
            home: Override agent home directory. If not set, resolves
                  from agent_name or SKCAPSTONE_AGENT env var.
            agent_name: Agent name (e.g. "lumina"). Used to resolve
                        per-agent home at ~/.skcapstone/agents/<name>/.
        """
        if home:
            self.home = home.expanduser()
        elif agent_name:
            self.home = agent_home(agent_name)
        else:
            self.home = agent_home()  # uses SKCAPSTONE_AGENT or falls back to root

        self.shared_root = shared_home()
        self.config = self._load_config()
        self.manifest = AgentManifest(
            home=self.home,
            version=__version__,
        )
        self._awakened = False

    def _load_config(self) -> AgentConfig:
        """Load agent configuration from disk.

        Checks per-agent config first, then falls back to shared config.

        Returns:
            AgentConfig loaded from config.yaml, or defaults.
        """
        for base in [self.home, self.shared_root]:
            config_file = base / "config" / "config.yaml"
            if config_file.exists():
                try:
                    data = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
                    return AgentConfig(**data)
                except (yaml.YAMLError, ValueError) as exc:
                    logger.warning("Failed to load config from %s: %s — trying next", base, exc)
        return AgentConfig()

    def awaken(self) -> AgentManifest:
        """Wake the agent up.

        Discovers all installed components from the per-agent home,
        with fallback to shared root for identity. Loads state from
        disk and builds the complete agent manifest.

        Returns:
            The fully populated AgentManifest.
        """
        logger.info("Awakening agent from %s (shared: %s)", self.home, self.shared_root)

        manifest_file = self.home / "manifest.json"
        manifest_name_loaded = False
        if manifest_file.exists():
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest_name = data.get("name")
                if manifest_name:
                    self.manifest.name = manifest_name
                    manifest_name_loaded = True
                if data.get("created_at"):
                    self.manifest.created_at = datetime.fromisoformat(data["created_at"])
                connectors_data = data.get("connectors", [])
                self.manifest.connectors = [ConnectorInfo(**c) for c in connectors_data]
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("Failed to load manifest: %s", exc)

        # Discover pillars from per-agent home
        pillars = discover_all(self.home, shared_root=self.shared_root)
        self.manifest.identity = pillars["identity"]
        self.manifest.memory = pillars["memory"]
        self.manifest.trust = pillars["trust"]
        self.manifest.consciousness = pillars["consciousness"]
        self.manifest.security = pillars["security"]
        self.manifest.sync = pillars["sync"]
        self.manifest.skills = pillars["skills"]

        if (
            self.manifest.identity.name
            and self.manifest.identity.status == PillarStatus.ACTIVE
        ):
            self.manifest.name = self.manifest.identity.name
        elif not manifest_name_loaded and self.config.agent_name:
            self.manifest.name = self.config.agent_name

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
        manifest_file.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def register_connector(self, name: str, platform: str) -> ConnectorInfo:
        """Register a platform connector."""
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

    def load_skills(self, agent: Optional[str] = None) -> Optional[object]:
        """Load SKSkills for this agent session via the SkillLoader."""
        try:
            from skskills.loader import SkillLoader
            from skskills.registry import SkillRegistry
        except ImportError:
            logger.debug("skskills not installed — skill loading unavailable")
            return None

        agent_name = agent or self.config.agent_name or "global"
        registry = SkillRegistry()
        loader = SkillLoader()

        skills = registry.list_skills(agent_name)
        if agent_name != "global":
            skills.extend(registry.list_skills("global"))

        loaded = 0
        seen: set[str] = set()
        for skill in skills:
            name = skill.manifest.name
            if name in seen:
                continue
            seen.add(name)
            try:
                loader.load(Path(skill.install_path))
                loaded += 1
            except Exception as exc:
                logger.warning("Failed to load skill '%s': %s", name, exc)

        self.manifest.skills.loaded = loaded
        self.manifest.skills.tools_available = len(loader.all_tools())

        logger.info(
            "Loaded %d skills for agent '%s' (%d tools available)",
            loaded, agent_name, self.manifest.skills.tools_available,
        )
        return loader

    @property
    def is_initialized(self) -> bool:
        """Check if the agent home has been initialized."""
        return self.home.exists() and (self.home / "config").exists()

    @property
    def is_conscious(self) -> bool:
        """Check if the agent has achieved consciousness."""
        return self.manifest.is_conscious


def get_runtime(
    home: Optional[Path] = None,
    agent_name: Optional[str] = None,
) -> AgentRuntime:
    """Get or create the global agent runtime.

    Args:
        home: Override agent home directory.
        agent_name: Agent name for per-agent path resolution.

    Returns:
        An initialized AgentRuntime.
    """
    runtime = AgentRuntime(home=home, agent_name=agent_name)
    if runtime.is_initialized:
        runtime.awaken()
    return runtime
