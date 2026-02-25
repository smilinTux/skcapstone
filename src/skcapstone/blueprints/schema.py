"""
Pydantic models for Agent Team Blueprint definitions.

A BlueprintManifest defines a complete deployable team of AI agents,
including their roles, models, resource requirements, networking,
memory, and coordination settings. Provider-agnostic by design —
the same blueprint deploys to local processes, Proxmox LXCs,
Hetzner, AWS, GCP, or any future provider.

Architecture references:
- Context isolation per agent (multi-agent-patterns best practice)
- Tiered model strategy (fast/code/reason/nuance)
- Memory-systems: file-system -> vector -> graph progression
- Hosted-agents: pre-built images, warm pools, snapshot/restore
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ModelTier(str, Enum):
    """Model selection tiers aligned with agent framework best practices."""

    FAST = "fast"
    CODE = "code"
    REASON = "reason"
    NUANCE = "nuance"
    LOCAL = "local"
    CUSTOM = "custom"


class ProviderType(str, Enum):
    """Infrastructure providers for agent deployment."""

    LOCAL = "local"
    PROXMOX = "proxmox"
    HETZNER = "hetzner"
    AWS = "aws"
    GCP = "gcp"
    DOCKER = "docker"


class VMType(str, Enum):
    """VM/container type for cloud/proxmox deployments."""

    LXC = "lxc"
    VM = "vm"
    CONTAINER = "container"
    PROCESS = "process"


class AgentRole(str, Enum):
    """Standard agent roles within a team."""

    MANAGER = "manager"
    WORKER = "worker"
    RESEARCHER = "researcher"
    CODER = "coder"
    REVIEWER = "reviewer"
    DOCUMENTARIAN = "documentarian"
    SECURITY = "security"
    OPS = "ops"
    CUSTOM = "custom"


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ResourceSpec(BaseModel):
    """Compute resources allocated to an agent."""

    memory: str = Field(default="2g", description="RAM allocation (e.g. '2g', '512m')")
    cores: int = Field(default=1, ge=1, le=128, description="CPU cores")
    disk: str = Field(default="10g", description="Disk allocation")
    gpu: Optional[str] = Field(default=None, description="GPU type if needed")


class AgentSpec(BaseModel):
    """Specification for a single agent within a team blueprint.

    Each agent gets its own context window (context isolation principle),
    its own model tier, its own soul blueprint, and a defined skill set.
    """

    role: AgentRole = Field(default=AgentRole.WORKER, description="Functional role")
    model: ModelTier = Field(default=ModelTier.FAST, description="Model tier")
    model_name: Optional[str] = Field(
        default=None,
        description="Specific model override (e.g. 'kimi-k2.5', 'minimax-m2.1')",
    )
    vm_type: VMType = Field(default=VMType.PROCESS, description="Execution environment")
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    soul_blueprint: Optional[str] = Field(
        default=None,
        description="Path to soul blueprint YAML (e.g. 'souls/sentinel.yaml')",
    )
    skills: List[str] = Field(default_factory=list, description="Skill names")
    depends_on: List[str] = Field(
        default_factory=list,
        description="Other agents this one depends on being ready first",
    )
    env: Dict[str, str] = Field(
        default_factory=dict,
        description="Extra environment variables for this agent",
    )
    description: Optional[str] = Field(default=None, description="What this agent does")
    count: int = Field(
        default=1, ge=1, le=50,
        description="Number of instances of this agent to spawn",
    )


class NetworkConfig(BaseModel):
    """Networking configuration for the team."""

    mesh_vpn: str = Field(default="tailscale", description="VPN mesh provider")
    discovery: str = Field(
        default="skref_registry",
        description="How agents discover each other",
    )


class StorageConfig(BaseModel):
    """Storage and memory configuration for the team."""

    skref_vault: Optional[str] = Field(
        default=None,
        description="Shared vault name for the team",
    )
    memory_backend: str = Field(
        default="filesystem",
        description="Memory backend: filesystem, skvector, mem0, zep",
    )
    memory_sync: bool = Field(
        default=True,
        description="Auto-sync agent memories to shared storage",
    )


class CoordinationConfig(BaseModel):
    """How the team coordinates internally."""

    queen: Optional[str] = Field(
        default=None,
        description="Managing agent (e.g. 'lumina')",
    )
    pattern: str = Field(
        default="supervisor",
        description="Architecture: supervisor, peer-to-peer, hierarchical",
    )
    heartbeat: str = Field(default="30m", description="Health check interval")
    escalation: Optional[str] = Field(
        default=None,
        description="Who to escalate critical issues to",
    )


# ---------------------------------------------------------------------------
# Top-level Blueprint
# ---------------------------------------------------------------------------

class BlueprintManifest(BaseModel):
    """Complete definition of a deployable agent team.

    This is the YAML schema that users select from the blueprint store.
    It contains everything needed to spin up a coordinated team of AI
    agents on any supported infrastructure provider.
    """

    name: str = Field(description="Human-readable team name")
    slug: str = Field(description="URL/filesystem-safe identifier")
    version: str = Field(default="1.0.0")
    description: str = Field(description="What this team does")
    icon: str = Field(default="🤖", description="Display emoji")
    author: str = Field(default="smilinTux")

    agents: Dict[str, AgentSpec] = Field(
        description="Named agents in this team (key = agent name)",
    )

    default_provider: ProviderType = Field(
        default=ProviderType.LOCAL,
        description="Default infrastructure provider",
    )
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    coordination: CoordinationConfig = Field(default_factory=CoordinationConfig)

    tags: List[str] = Field(default_factory=list)
    estimated_cost: Optional[str] = Field(
        default=None,
        description="Estimated monthly cost (e.g. '$12/mo compute')",
    )

    @field_validator("slug")
    @classmethod
    def slug_must_be_clean(cls, v: str) -> str:
        """Ensure slug is filesystem/URL safe."""
        import re
        if not re.match(r"^[a-z0-9][a-z0-9\-]*[a-z0-9]$", v):
            raise ValueError(
                f"slug must be lowercase alphanumeric with hyphens: got '{v}'"
            )
        return v

    @property
    def agent_count(self) -> int:
        """Total number of agent instances in this blueprint."""
        return sum(spec.count for spec in self.agents.values())

    @property
    def model_summary(self) -> str:
        """Comma-separated list of model tiers used."""
        tiers = sorted({
            spec.model_name or spec.model.value
            for spec in self.agents.values()
        })
        return ", ".join(tiers)
