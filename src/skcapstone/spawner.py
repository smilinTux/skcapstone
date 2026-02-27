"""
Sub-agent Spawner — spin up task-specific agents on correct nodes.

Unlike the TeamEngine which deploys full teams from blueprints, the
spawner creates lightweight single-purpose agents for specific tasks.
It auto-selects the right model tier and role based on the task
description, then provisions via the appropriate provider.

Usage:
    spawner = SubAgentSpawner(home=Path("~/.skcapstone"))
    result = spawner.spawn(
        task="Write unit tests for the capauth login flow",
        provider=ProviderType.LOCAL,
    )
    # result.agent_name, result.deployment_id, result.status

The spawner integrates with the coordination board to optionally
claim tasks for spawned agents and with the trustee audit log for
transparency.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .blueprints.schema import (
    AgentRole,
    AgentSpec,
    BlueprintManifest,
    CoordinationConfig,
    ModelTier,
    NetworkConfig,
    ProviderType,
    ResourceSpec,
    StorageConfig,
    VMType,
)
from .team_engine import AgentStatus, DeployedAgent, ProviderBackend, TeamEngine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task classification
# ---------------------------------------------------------------------------

# Keywords mapped to (role, model_tier) tuples for auto-detection
_TASK_ROLE_MAP: Dict[str, tuple[AgentRole, ModelTier]] = {
    "test": (AgentRole.CODER, ModelTier.CODE),
    "unit test": (AgentRole.CODER, ModelTier.CODE),
    "debug": (AgentRole.CODER, ModelTier.CODE),
    "fix bug": (AgentRole.CODER, ModelTier.CODE),
    "implement": (AgentRole.CODER, ModelTier.CODE),
    "refactor": (AgentRole.CODER, ModelTier.CODE),
    "code review": (AgentRole.REVIEWER, ModelTier.REASON),
    "review": (AgentRole.REVIEWER, ModelTier.REASON),
    "architecture": (AgentRole.REVIEWER, ModelTier.REASON),
    "design": (AgentRole.REVIEWER, ModelTier.REASON),
    "plan": (AgentRole.REVIEWER, ModelTier.REASON),
    "research": (AgentRole.RESEARCHER, ModelTier.REASON),
    "analyze": (AgentRole.RESEARCHER, ModelTier.REASON),
    "investigate": (AgentRole.RESEARCHER, ModelTier.REASON),
    "explore": (AgentRole.RESEARCHER, ModelTier.FAST),
    "document": (AgentRole.DOCUMENTARIAN, ModelTier.FAST),
    "write docs": (AgentRole.DOCUMENTARIAN, ModelTier.FAST),
    "readme": (AgentRole.DOCUMENTARIAN, ModelTier.FAST),
    "changelog": (AgentRole.DOCUMENTARIAN, ModelTier.FAST),
    "security audit": (AgentRole.SECURITY, ModelTier.REASON),
    "vulnerability": (AgentRole.SECURITY, ModelTier.REASON),
    "scan": (AgentRole.SECURITY, ModelTier.FAST),
    "deploy": (AgentRole.OPS, ModelTier.FAST),
    "monitor": (AgentRole.OPS, ModelTier.FAST),
    "backup": (AgentRole.OPS, ModelTier.FAST),
}


def classify_task(description: str) -> tuple[AgentRole, ModelTier]:
    """Determine the best agent role and model tier for a task description.

    Scans the description for known keywords and returns the most specific
    match. Falls back to (WORKER, FAST) if nothing matches.

    Args:
        description: Free-text task description.

    Returns:
        Tuple of (AgentRole, ModelTier).
    """
    desc_lower = description.lower()

    # Try multi-word patterns first (more specific)
    for pattern in sorted(_TASK_ROLE_MAP.keys(), key=len, reverse=True):
        if pattern in desc_lower:
            return _TASK_ROLE_MAP[pattern]

    return (AgentRole.WORKER, ModelTier.FAST)


# ---------------------------------------------------------------------------
# Node selection
# ---------------------------------------------------------------------------

class NodeInfo(BaseModel):
    """Describes an available deployment target node."""

    name: str
    provider: ProviderType
    host: str = "localhost"
    capacity: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: List[str] = Field(default_factory=list)


def select_node(
    nodes: List[NodeInfo],
    role: AgentRole,
    model: ModelTier,
    preferred_provider: Optional[ProviderType] = None,
) -> NodeInfo:
    """Select the best node for a given agent role and model.

    Selection criteria:
    1. Preferred provider match
    2. Highest available capacity
    3. Tag affinity (e.g. "gpu" nodes for reason-tier models)

    Args:
        nodes: Available deployment nodes.
        role: The agent's role.
        model: The model tier required.
        preferred_provider: If set, prefer nodes of this provider type.

    Returns:
        The selected NodeInfo.
    """
    if not nodes:
        return NodeInfo(name="local", provider=ProviderType.LOCAL)

    scored: List[tuple[float, NodeInfo]] = []
    for node in nodes:
        score = node.capacity

        if preferred_provider and node.provider == preferred_provider:
            score += 2.0

        if model in (ModelTier.REASON, ModelTier.NUANCE) and "gpu" in node.tags:
            score += 1.0

        if model == ModelTier.LOCAL and node.provider == ProviderType.LOCAL:
            score += 1.5

        scored.append((score, node))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]


# ---------------------------------------------------------------------------
# Spawn result
# ---------------------------------------------------------------------------

class SpawnResult(BaseModel):
    """Result of spawning a sub-agent."""

    agent_name: str
    deployment_id: str
    status: AgentStatus
    provider: ProviderType
    host: str = "localhost"
    pid: Optional[int] = None
    role: AgentRole = AgentRole.WORKER
    model: ModelTier = ModelTier.FAST
    task_description: str = ""
    coord_task_id: Optional[str] = None
    spawned_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Sub-agent spawner
# ---------------------------------------------------------------------------

class SubAgentSpawner:
    """Lightweight spawner for task-specific sub-agents.

    Wraps TeamEngine to create single-agent deployments for specific tasks
    without requiring a full blueprint. Integrates with the coordination
    board and trustee audit trail.

    Args:
        home: Agent home directory.
        provider: Provider backend (defaults to LocalProvider if None).
        nodes: Available deployment nodes for smart routing.
    """

    def __init__(
        self,
        home: Optional[Path] = None,
        provider: Optional[ProviderBackend] = None,
        nodes: Optional[List[NodeInfo]] = None,
    ) -> None:
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._provider = provider
        self._nodes = nodes or []
        self._engine = TeamEngine(
            home=self._home,
            provider=self._provider,
            comms_root=self._home / "comms",
        )

    def _build_mini_blueprint(
        self,
        task: str,
        role: AgentRole,
        model: ModelTier,
        skills: Optional[List[str]] = None,
        soul_blueprint: Optional[str] = None,
    ) -> BlueprintManifest:
        """Create a minimal single-agent blueprint for a task.

        Args:
            task: Task description (used for naming).
            role: Agent role.
            model: Model tier.
            skills: Optional skill list.
            soul_blueprint: Optional soul blueprint path.

        Returns:
            A BlueprintManifest with one agent.
        """
        slug = f"spawn-{role.value}-{int(time.time())}"
        agent_key = f"{role.value}-agent"

        spec = AgentSpec(
            role=role,
            model=model,
            skills=skills or [],
            soul_blueprint=soul_blueprint,
            description=task[:200],
            count=1,
        )

        return BlueprintManifest(
            name=f"Spawned {role.value} for: {task[:80]}",
            slug=slug,
            version="0.1.0",
            description=task,
            icon=_role_icon(role),
            author="sub-agent-spawner",
            agents={agent_key: spec},
            default_provider=ProviderType.LOCAL,
            network=NetworkConfig(
                mesh_vpn="tailscale",
                discovery="skref_registry",
            ),
            storage=StorageConfig(
                memory_backend="filesystem",
                memory_sync=False,
            ),
            coordination=CoordinationConfig(
                pattern="peer-to-peer",
                heartbeat="5m",
            ),
            tags=["spawned", "sub-agent", role.value],
        )

    def spawn(
        self,
        task: str,
        provider: Optional[ProviderType] = None,
        role: Optional[AgentRole] = None,
        model: Optional[ModelTier] = None,
        skills: Optional[List[str]] = None,
        soul_blueprint: Optional[str] = None,
        coord_task_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> SpawnResult:
        """Spawn a task-specific sub-agent.

        Auto-classifies the task to determine role and model tier if not
        explicitly provided. Selects the best node, builds a mini blueprint,
        and deploys via TeamEngine.

        Args:
            task: Free-text task description.
            provider: Force a specific provider type.
            role: Override auto-detected agent role.
            model: Override auto-detected model tier.
            skills: Skills to load into the agent.
            soul_blueprint: Path to a soul blueprint YAML.
            coord_task_id: Optional coordination board task to claim.
            agent_name: Optional custom agent name.

        Returns:
            SpawnResult with deployment details.
        """
        # Auto-classify if not specified
        detected_role, detected_model = classify_task(task)
        final_role = role or detected_role
        final_model = model or detected_model

        # Select node
        node = select_node(
            self._nodes, final_role, final_model,
            preferred_provider=provider,
        )
        target_provider = provider or node.provider

        logger.info(
            "Spawning %s agent (model=%s) on %s for: %s",
            final_role.value, final_model.value, node.name, task[:80],
        )

        # Build mini blueprint
        blueprint = self._build_mini_blueprint(
            task, final_role, final_model, skills, soul_blueprint,
        )

        # Deploy
        try:
            deployment = self._engine.deploy(
                blueprint,
                name=agent_name,
                provider_override=target_provider,
            )

            # Get the single deployed agent
            agents = list(deployment.agents.values())
            agent = agents[0] if agents else None

            if not agent:
                return SpawnResult(
                    agent_name=agent_name or "unknown",
                    deployment_id=deployment.deployment_id,
                    status=AgentStatus.FAILED,
                    provider=target_provider,
                    role=final_role,
                    model=final_model,
                    task_description=task,
                    error="No agent was created in deployment.",
                )

            # Claim coordination task if provided
            if coord_task_id:
                self._claim_coord_task(coord_task_id, agent.name)

            # Audit the spawn
            self._audit_spawn(agent, task, deployment.deployment_id)

            return SpawnResult(
                agent_name=agent.name,
                deployment_id=deployment.deployment_id,
                status=agent.status,
                provider=target_provider,
                host=agent.host or "localhost",
                pid=agent.pid,
                role=final_role,
                model=final_model,
                task_description=task,
                coord_task_id=coord_task_id,
            )

        except Exception as exc:
            logger.error("Spawn failed: %s", exc)
            return SpawnResult(
                agent_name=agent_name or f"spawn-{final_role.value}-failed",
                deployment_id="",
                status=AgentStatus.FAILED,
                provider=target_provider,
                role=final_role,
                model=final_model,
                task_description=task,
                error=str(exc),
            )

    def spawn_batch(
        self,
        tasks: List[Dict[str, Any]],
        provider: Optional[ProviderType] = None,
    ) -> List[SpawnResult]:
        """Spawn multiple sub-agents for a batch of tasks.

        Args:
            tasks: List of dicts with at least a "task" key. Optional keys:
                "role", "model", "skills", "coord_task_id".
            provider: Default provider for all spawns.

        Returns:
            List of SpawnResult, one per task.
        """
        results = []
        for task_spec in tasks:
            result = self.spawn(
                task=task_spec["task"],
                provider=provider or task_spec.get("provider"),
                role=task_spec.get("role"),
                model=task_spec.get("model"),
                skills=task_spec.get("skills"),
                coord_task_id=task_spec.get("coord_task_id"),
            )
            results.append(result)
        return results

    def list_spawned(self) -> List[SpawnResult]:
        """List all currently spawned sub-agents.

        Returns:
            List of SpawnResult for active spawned deployments.
        """
        results = []
        for dep in self._engine.list_deployments():
            if "spawned" not in dep.blueprint_slug.split("-")[0:1]:
                # Only show spawn-prefixed deployments
                if not dep.blueprint_slug.startswith("spawn-"):
                    continue

            for agent in dep.agents.values():
                results.append(SpawnResult(
                    agent_name=agent.name,
                    deployment_id=dep.deployment_id,
                    status=agent.status,
                    provider=dep.provider,
                    host=agent.host or "localhost",
                    pid=agent.pid,
                    task_description=dep.team_name or "",
                    spawned_at=agent.started_at or dep.created_at,
                ))
        return results

    def kill(self, deployment_id: str) -> bool:
        """Destroy a spawned sub-agent by deployment ID.

        Args:
            deployment_id: The deployment to destroy.

        Returns:
            True if successfully destroyed.
        """
        return self._engine.destroy_deployment(deployment_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _claim_coord_task(self, task_id: str, agent_name: str) -> None:
        """Claim a coordination board task for the spawned agent."""
        try:
            from .coordination import Board
            board = Board(home=self._home)
            board.claim_task(agent_name, task_id)
            logger.info("Claimed coord task %s for %s", task_id, agent_name)
        except Exception as exc:
            logger.warning("Could not claim coord task %s: %s", task_id, exc)

    def _audit_spawn(
        self,
        agent: DeployedAgent,
        task: str,
        deployment_id: str,
    ) -> None:
        """Write an audit entry for the spawn."""
        try:
            from ._trustee_helpers import write_audit
            write_audit(
                "spawn_agent",
                deployment_id,
                {
                    "agent_name": agent.name,
                    "task": task[:200],
                    "status": agent.status.value,
                    "provider": agent.provider.value,
                },
                home=self._home,
            )
        except Exception as exc:
            logger.warning("Audit write failed: %s", exc)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _role_icon(role: AgentRole) -> str:
    """Map agent role to an emoji icon."""
    return {
        AgentRole.MANAGER: "👑",
        AgentRole.WORKER: "⚙️",
        AgentRole.RESEARCHER: "🔬",
        AgentRole.CODER: "💻",
        AgentRole.REVIEWER: "🔍",
        AgentRole.DOCUMENTARIAN: "📝",
        AgentRole.SECURITY: "🛡️",
        AgentRole.OPS: "🔧",
        AgentRole.CUSTOM: "🎯",
    }.get(role, "🤖")
