"""
Team Deployment Engine — provider-agnostic orchestration.

Takes a BlueprintManifest and deploys it to the target infrastructure.
The engine doesn't care whether agents land on local processes, Proxmox
LXCs, Hetzner VMs, AWS, GCP, or Docker containers. Each provider
implements the same interface; the engine orchestrates the workflow.

Architecture follows best practices from:
- multi-agent-patterns: context isolation, supervisor coordination
- hosted-agents: pre-built images, warm pools, snapshot/restore
- memory-systems: filesystem-first, upgrade path to vector/graph

Deployment flow:
  1. Validate blueprint + resolve dependencies
  2. Provision infrastructure (provider-specific)
  3. Configure each agent (soul, skills, memory, network)
  4. Register in vault registry (skref)
  5. Start health monitoring
  6. Hand off to coordination layer (queen)
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .blueprints.schema import AgentRole, AgentSpec, BlueprintManifest, ProviderType
from .team_comms import TeamChannel, bootstrap_team_channel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Deployment state
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    """Runtime status of a deployed agent."""

    PENDING = "pending"
    PROVISIONING = "provisioning"
    CONFIGURING = "configuring"
    RUNNING = "running"
    DEGRADED = "degraded"
    STOPPED = "stopped"
    FAILED = "failed"


class DeployedAgent(BaseModel):
    """Runtime state of a single deployed agent instance."""

    name: str
    instance_id: str
    blueprint_slug: str
    agent_spec_key: str
    status: AgentStatus = AgentStatus.PENDING
    provider: ProviderType = ProviderType.LOCAL
    host: Optional[str] = None
    port: Optional[int] = None
    pid: Optional[int] = None
    container_id: Optional[str] = None
    started_at: Optional[str] = None
    last_heartbeat: Optional[str] = None
    error: Optional[str] = None


class TeamDeployment(BaseModel):
    """Full state of a deployed team."""

    deployment_id: str
    blueprint_slug: str
    team_name: str
    provider: ProviderType
    agents: Dict[str, DeployedAgent] = Field(default_factory=dict)
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = "deploying"
    comms_channel: Optional[Any] = Field(
        default=None,
        exclude=True,
        description="In-memory TeamChannel; not persisted to disk.",
    )


# ---------------------------------------------------------------------------
# Provider interface (abstract base)
# ---------------------------------------------------------------------------

class ProviderBackend:
    """Abstract base for infrastructure providers.

    Each provider (local, proxmox, hetzner, aws, gcp, docker) implements
    these methods. The engine calls them in sequence during deployment.
    """

    provider_type: ProviderType = ProviderType.LOCAL

    def provision(
        self,
        agent_name: str,
        spec: AgentSpec,
        team_name: str,
    ) -> Dict[str, Any]:
        """Provision infrastructure for one agent.

        Args:
            agent_name: Unique name for this agent instance.
            spec: The agent specification from the blueprint.
            team_name: The team this agent belongs to.

        Returns:
            Dict with provider-specific details (host, port, pid, container_id, etc.)
        """
        raise NotImplementedError

    def configure(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Configure an agent after provisioning (soul, skills, memory).

        Args:
            agent_name: The agent instance name.
            spec: The agent specification.
            provision_result: Output from provision().

        Returns:
            True if configuration succeeded.
        """
        raise NotImplementedError

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Start the agent process/container.

        Args:
            agent_name: The agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the agent started successfully.
        """
        raise NotImplementedError

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Stop a running agent.

        Args:
            agent_name: The agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the agent stopped successfully.
        """
        raise NotImplementedError

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Destroy agent infrastructure entirely.

        Args:
            agent_name: The agent instance name.
            provision_result: Output from provision().

        Returns:
            True if destruction succeeded.
        """
        raise NotImplementedError

    def health_check(self, agent_name: str, provision_result: Dict[str, Any]) -> AgentStatus:
        """Check the health of a deployed agent.

        Args:
            agent_name: The agent instance name.
            provision_result: Output from provision().

        Returns:
            Current AgentStatus.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Deployment engine
# ---------------------------------------------------------------------------

class TeamEngine:
    """Orchestrates team deployment across any provider.

    Args:
        home: Agent home directory.
        provider: The backend to deploy to.
        comms_root: Root directory for team comms channels. Defaults to
            ``<home>/comms``. Pass ``None`` to disable comms bootstrapping.
    """

    def __init__(
        self,
        home: Optional[Path] = None,
        provider: Optional[ProviderBackend] = None,
        comms_root: Optional[Path] = None,
    ) -> None:
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._provider = provider
        self._deployments_dir = self._home / "deployments"
        self._deployments_dir.mkdir(parents=True, exist_ok=True)
        # Reason: allow callers to disable comms by passing comms_root=None explicitly
        self._comms_root: Optional[Path] = (
            comms_root if comms_root is not None else self._home / "comms"
        )

    # ------------------------------------------------------------------
    # Dependency resolution
    # ------------------------------------------------------------------

    @staticmethod
    def resolve_deploy_order(blueprint: BlueprintManifest) -> List[List[str]]:
        """Topological sort of agents by depends_on.

        Returns a list of "waves" — agents in the same wave can deploy
        in parallel; each wave must complete before the next starts.

        Args:
            blueprint: The team blueprint.

        Returns:
            List of waves, each wave is a list of agent keys.

        Raises:
            ValueError: If circular dependencies are detected.
        """
        agent_keys = set(blueprint.agents.keys())
        remaining = dict(blueprint.agents)
        waves: List[List[str]] = []
        resolved: set = set()

        max_iterations = len(agent_keys) + 1
        iteration = 0

        while remaining:
            iteration += 1
            if iteration > max_iterations:
                unresolved = list(remaining.keys())
                raise ValueError(
                    f"Circular dependency detected among: {unresolved}"
                )

            wave = []
            for key, spec in list(remaining.items()):
                deps = set(spec.depends_on) & agent_keys
                if deps <= resolved:
                    wave.append(key)

            if not wave:
                unresolved = list(remaining.keys())
                raise ValueError(
                    f"Unresolvable dependencies for: {unresolved}"
                )

            for key in wave:
                del remaining[key]
                resolved.add(key)

            waves.append(wave)

        return waves

    # ------------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------------

    def deploy(
        self,
        blueprint: BlueprintManifest,
        name: Optional[str] = None,
        provider_override: Optional[ProviderType] = None,
    ) -> TeamDeployment:
        """Deploy a team from a blueprint.

        Args:
            blueprint: The validated blueprint manifest.
            name: Optional custom deployment name.
            provider_override: Override the blueprint's default provider.

        Returns:
            TeamDeployment with the full state of all agents.
        """
        deployment_id = f"{blueprint.slug}-{int(time.time())}"
        team_name = name or blueprint.name
        provider_type = provider_override or blueprint.default_provider

        deployment = TeamDeployment(
            deployment_id=deployment_id,
            blueprint_slug=blueprint.slug,
            team_name=team_name,
            provider=provider_type,
        )

        waves = self.resolve_deploy_order(blueprint)
        logger.info(
            "Deploying %s: %d agents in %d waves",
            team_name, blueprint.agent_count, len(waves),
        )

        for wave_idx, wave in enumerate(waves):
            logger.info("Wave %d: %s", wave_idx + 1, wave)

            for agent_key in wave:
                spec = blueprint.agents[agent_key]
                for instance_num in range(spec.count):
                    suffix = f"-{instance_num + 1}" if spec.count > 1 else ""
                    instance_name = f"{blueprint.slug}-{agent_key}{suffix}"

                    deployed = DeployedAgent(
                        name=instance_name,
                        instance_id=f"{deployment_id}/{instance_name}",
                        blueprint_slug=blueprint.slug,
                        agent_spec_key=agent_key,
                        provider=provider_type,
                        status=AgentStatus.PROVISIONING,
                    )

                    if self._provider:
                        try:
                            result = self._provider.provision(
                                instance_name, spec, team_name,
                            )
                            deployed.host = result.get("host")
                            deployed.port = result.get("port")
                            deployed.pid = result.get("pid")
                            deployed.container_id = result.get("container_id")

                            deployed.status = AgentStatus.CONFIGURING
                            self._provider.configure(instance_name, spec, result)

                            deployed.status = AgentStatus.RUNNING
                            self._provider.start(instance_name, result)
                            deployed.started_at = datetime.now(
                                timezone.utc
                            ).isoformat()
                            deployed.last_heartbeat = deployed.started_at

                        except Exception as exc:
                            deployed.status = AgentStatus.FAILED
                            deployed.error = str(exc)
                            logger.error(
                                "Failed to deploy %s: %s", instance_name, exc
                            )
                    else:
                        # Dry-run mode: no provider, just record the plan
                        deployed.status = AgentStatus.PENDING
                        deployed.host = "localhost"

                    deployment.agents[instance_name] = deployed

        deployment.status = (
            "running"
            if all(
                a.status == AgentStatus.RUNNING or a.status == AgentStatus.PENDING
                for a in deployment.agents.values()
            )
            else "degraded"
        )

        self._save_deployment(deployment)

        if self._comms_root is not None:
            deployment.comms_channel = self._bootstrap_comms(
                deployment, blueprint
            )

        return deployment

    # ------------------------------------------------------------------
    # Status / management
    # ------------------------------------------------------------------

    def list_deployments(self) -> List[TeamDeployment]:
        """List all saved deployments.

        Returns:
            List of TeamDeployment objects.
        """
        deployments = []
        for f in sorted(self._deployments_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                deployments.append(TeamDeployment(**data))
            except Exception as exc:
                logger.warning("Skipping %s: %s", f, exc)
        return deployments

    def get_deployment(self, deployment_id: str) -> Optional[TeamDeployment]:
        """Load a specific deployment by ID.

        Args:
            deployment_id: The deployment identifier.

        Returns:
            TeamDeployment or None.
        """
        path = self._deployments_dir / f"{deployment_id}.json"
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return TeamDeployment(**data)

    def destroy_deployment(self, deployment_id: str) -> bool:
        """Destroy all agents in a deployment and remove state.

        Args:
            deployment_id: The deployment to destroy.

        Returns:
            True if all agents were destroyed.
        """
        deployment = self.get_deployment(deployment_id)
        if not deployment:
            return False

        all_ok = True
        if self._provider:
            for agent in deployment.agents.values():
                try:
                    self._provider.destroy(
                        agent.name,
                        {"host": agent.host, "pid": agent.pid,
                         "container_id": agent.container_id},
                    )
                except Exception as exc:
                    logger.error("Failed to destroy %s: %s", agent.name, exc)
                    all_ok = False

        path = self._deployments_dir / f"{deployment_id}.json"
        if path.exists():
            path.unlink()

        return all_ok

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_deployment(self, deployment: TeamDeployment) -> Path:
        """Save deployment state to disk.

        Args:
            deployment: The deployment to persist.

        Returns:
            Path to the saved JSON file.
        """
        path = self._deployments_dir / f"{deployment.deployment_id}.json"
        path.write_text(
            json.dumps(deployment.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )
        return path

    def _bootstrap_comms(
        self,
        deployment: TeamDeployment,
        blueprint: BlueprintManifest,
    ) -> TeamChannel:
        """Bootstrap SKComm file channel for all agents in a deployment.

        Identifies the queen agent from the blueprint's coordination config
        (falling back to any agent with role=manager) and provisions per-agent
        inboxes plus a broadcast directory.

        Args:
            deployment: The freshly created TeamDeployment.
            blueprint: The blueprint manifest used for this deployment.

        Returns:
            TeamChannel: The configured comms channel.
        """
        agent_names = list(deployment.agents.keys())

        # Determine the queen: prefer coordination config, then role=manager
        queen: Optional[str] = None
        configured_queen = blueprint.coordination.queen
        if configured_queen:
            # Find the deployed instance that matches this spec key
            for inst_name in agent_names:
                agent = deployment.agents[inst_name]
                if agent.agent_spec_key == configured_queen:
                    queen = inst_name
                    break

        if queen is None:
            for inst_name in agent_names:
                agent = deployment.agents[inst_name]
                spec = blueprint.agents.get(agent.agent_spec_key)
                if spec and spec.role == AgentRole.MANAGER:
                    queen = inst_name
                    break

        assert self._comms_root is not None  # guarded by caller
        channel = bootstrap_team_channel(
            team_slug=deployment.deployment_id,
            agent_names=agent_names,
            comms_root=self._comms_root,
            queen=queen,
        )

        logger.info(
            "Comms channel ready for deployment '%s' (queen=%s)",
            deployment.deployment_id,
            queen or "none",
        )
        return channel
