"""Tests for agent team blueprints — schema, registry, and engine."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from skcapstone.blueprints.schema import (
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
from skcapstone.blueprints.registry import BlueprintRegistry
from skcapstone.team_engine import (
    AgentStatus,
    DeployedAgent,
    TeamDeployment,
    TeamEngine,
)
from skcapstone.providers.local import LocalProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _minimal_blueprint(**overrides) -> dict:
    """Build a minimal valid blueprint dict."""
    base = {
        "name": "Test Team",
        "slug": "test-team",
        "description": "A test blueprint.",
        "agents": {
            "alpha": {
                "role": "worker",
                "model": "fast",
                "skills": ["python"],
            },
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def tmp_home(tmp_path):
    """Create a temporary agent home directory."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return home


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestBlueprintSchema:
    """Validate the BlueprintManifest pydantic model."""

    def test_minimal_blueprint_parses(self):
        """Minimal blueprint with one agent should parse."""
        bp = BlueprintManifest(**_minimal_blueprint())
        assert bp.name == "Test Team"
        assert bp.slug == "test-team"
        assert bp.agent_count == 1
        assert "alpha" in bp.agents

    def test_agent_count_with_multiple_instances(self):
        """Agent count should sum up instance counts."""
        data = _minimal_blueprint()
        data["agents"]["alpha"]["count"] = 3
        data["agents"]["beta"] = {"role": "coder", "model": "code", "count": 2}
        bp = BlueprintManifest(**data)
        assert bp.agent_count == 5

    def test_model_summary(self):
        """Model summary should list unique model tiers."""
        data = _minimal_blueprint()
        data["agents"]["beta"] = {"role": "coder", "model": "code", "model_name": "minimax-m2.1"}
        bp = BlueprintManifest(**data)
        assert "fast" in bp.model_summary
        assert "minimax-m2.1" in bp.model_summary

    def test_slug_validation_rejects_uppercase(self):
        """Slug must be lowercase with hyphens only."""
        with pytest.raises(ValueError, match="slug must be lowercase"):
            BlueprintManifest(**_minimal_blueprint(slug="Bad_Slug"))

    def test_slug_validation_rejects_special_chars(self):
        """Slug rejects special characters."""
        with pytest.raises(ValueError):
            BlueprintManifest(**_minimal_blueprint(slug="bad slug!"))

    def test_defaults_are_sensible(self):
        """Verify default values for optional fields."""
        bp = BlueprintManifest(**_minimal_blueprint())
        assert bp.default_provider == ProviderType.LOCAL
        assert bp.network.mesh_vpn == "tailscale"
        assert bp.storage.memory_backend == "filesystem"
        assert bp.coordination.pattern == "supervisor"
        assert bp.version == "1.0.0"

    def test_agent_spec_defaults(self):
        """AgentSpec should have sensible defaults."""
        spec = AgentSpec()
        assert spec.role == AgentRole.WORKER
        assert spec.model == ModelTier.FAST
        assert spec.vm_type == VMType.PROCESS
        assert spec.count == 1
        assert spec.resources.memory == "2g"
        assert spec.resources.cores == 1

    def test_resource_spec_constraints(self):
        """Cores must be >= 1 and <= 128."""
        with pytest.raises(ValueError):
            ResourceSpec(cores=0)
        with pytest.raises(ValueError):
            ResourceSpec(cores=200)

    def test_full_blueprint_with_all_fields(self):
        """A fully-specified blueprint should parse."""
        data = _minimal_blueprint(
            version="2.0.0",
            icon="🛡️",
            author="chef",
            default_provider="proxmox",
            estimated_cost="$15/mo",
            tags=["security", "ops"],
            network={"mesh_vpn": "tailscale", "discovery": "skref_registry"},
            storage={"skref_vault": "my-vault", "memory_backend": "skvector"},
            coordination={
                "queen": "lumina",
                "pattern": "hierarchical",
                "heartbeat": "5m",
                "escalation": "chef",
            },
        )
        bp = BlueprintManifest(**data)
        assert bp.default_provider == ProviderType.PROXMOX
        assert bp.coordination.queen == "lumina"
        assert bp.storage.memory_backend == "skvector"


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestBlueprintRegistry:
    """Test blueprint discovery and loading."""

    def test_scan_finds_builtins(self):
        """Registry should find built-in blueprints."""
        registry = BlueprintRegistry()
        blueprints = registry.list_blueprints()
        slugs = [bp.slug for bp in blueprints]
        assert "infrastructure-guardian" in slugs
        assert "dev-squadron" in slugs
        assert "research-pod" in slugs
        assert len(blueprints) >= 6

    def test_get_by_slug(self):
        """Get a specific blueprint by slug."""
        registry = BlueprintRegistry()
        bp = registry.get("infrastructure-guardian")
        assert bp is not None
        assert bp.name == "Infrastructure Guardian"
        assert bp.agent_count >= 6

    def test_get_missing_returns_none(self):
        """Missing slug should return None."""
        registry = BlueprintRegistry()
        assert registry.get("nonexistent-team") is None

    def test_user_blueprints_override_builtins(self, tmp_home):
        """User blueprints should take priority over built-ins."""
        user_dir = tmp_home / "blueprints" / "teams"
        user_dir.mkdir(parents=True)

        custom = _minimal_blueprint(
            name="Custom Guardian",
            slug="infrastructure-guardian",
            description="My custom override.",
        )
        (user_dir / "infrastructure-guardian.yaml").write_text(
            yaml.dump(custom), encoding="utf-8"
        )

        registry = BlueprintRegistry(home=tmp_home)
        bp = registry.get("infrastructure-guardian")
        assert bp is not None
        assert bp.name == "Custom Guardian"

    def test_save_blueprint(self, tmp_home):
        """Saving a blueprint should write a YAML file."""
        registry = BlueprintRegistry(home=tmp_home)
        bp = BlueprintManifest(**_minimal_blueprint())
        path = registry.save_blueprint(bp)

        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert loaded["name"] == "Test Team"

    def test_invalid_yaml_is_skipped(self, tmp_home):
        """Invalid YAML files should be skipped with a warning."""
        user_dir = tmp_home / "blueprints" / "teams"
        user_dir.mkdir(parents=True)
        (user_dir / "broken.yaml").write_text("not: a: valid: blueprint: [")

        registry = BlueprintRegistry(home=tmp_home)
        # Should not raise
        blueprints = registry.list_blueprints()
        assert isinstance(blueprints, list)


# ---------------------------------------------------------------------------
# Team Engine tests
# ---------------------------------------------------------------------------


class TestTeamEngine:
    """Test deployment orchestration."""

    def test_resolve_deploy_order_simple(self):
        """Simple dependencies should resolve in correct order."""
        bp = BlueprintManifest(**_minimal_blueprint(
            agents={
                "alpha": {"role": "manager", "model": "fast"},
                "beta": {"role": "worker", "model": "code", "depends_on": ["alpha"]},
                "gamma": {"role": "worker", "model": "code", "depends_on": ["alpha", "beta"]},
            }
        ))

        waves = TeamEngine.resolve_deploy_order(bp)
        assert waves[0] == ["alpha"]
        assert "beta" in waves[1]
        assert "gamma" in waves[2] if len(waves) > 2 else "gamma" in waves[1]

    def test_resolve_deploy_order_parallel(self):
        """Independent agents should be in the same wave."""
        bp = BlueprintManifest(**_minimal_blueprint(
            agents={
                "alpha": {"role": "worker", "model": "fast"},
                "beta": {"role": "worker", "model": "code"},
                "gamma": {"role": "worker", "model": "local"},
            }
        ))

        waves = TeamEngine.resolve_deploy_order(bp)
        assert len(waves) == 1
        assert set(waves[0]) == {"alpha", "beta", "gamma"}

    def test_resolve_circular_dependency_raises(self):
        """Circular dependencies should raise ValueError."""
        bp = BlueprintManifest(**_minimal_blueprint(
            agents={
                "alpha": {"role": "worker", "model": "fast", "depends_on": ["beta"]},
                "beta": {"role": "worker", "model": "code", "depends_on": ["alpha"]},
            }
        ))

        with pytest.raises(ValueError, match="[Cc]ircular|[Uu]nresolvable"):
            TeamEngine.resolve_deploy_order(bp)

    def test_deploy_dry_run(self, tmp_home):
        """Deploy without a provider should create pending agents."""
        bp = BlueprintManifest(**_minimal_blueprint())
        engine = TeamEngine(home=tmp_home, provider=None)

        deployment = engine.deploy(bp)
        assert deployment.blueprint_slug == "test-team"
        assert len(deployment.agents) == 1

        agent = list(deployment.agents.values())[0]
        assert agent.status == AgentStatus.PENDING
        assert agent.host == "localhost"

    def test_deploy_with_local_provider(self, tmp_home):
        """Deploy with LocalProvider should create agent directories."""
        bp = BlueprintManifest(**_minimal_blueprint())
        backend = LocalProvider(home=tmp_home)
        engine = TeamEngine(home=tmp_home, provider=backend)

        deployment = engine.deploy(bp)
        assert deployment.status == "running"

        agent = list(deployment.agents.values())[0]
        assert agent.status == AgentStatus.RUNNING
        assert agent.host == "localhost"

        # Check agent directory was created
        agent_dir = tmp_home / "agents" / "local" / agent.name
        assert agent_dir.exists()
        assert (agent_dir / "config.json").exists()

    def test_list_deployments(self, tmp_home):
        """List deployments should return saved state."""
        bp = BlueprintManifest(**_minimal_blueprint())
        engine = TeamEngine(home=tmp_home)

        engine.deploy(bp)
        deployments = engine.list_deployments()
        assert len(deployments) == 1
        assert deployments[0].blueprint_slug == "test-team"

    def test_destroy_deployment(self, tmp_home):
        """Destroying a deployment should remove the state file."""
        bp = BlueprintManifest(**_minimal_blueprint())
        engine = TeamEngine(home=tmp_home)

        deployment = engine.deploy(bp)
        assert len(engine.list_deployments()) == 1

        engine.destroy_deployment(deployment.deployment_id)
        assert len(engine.list_deployments()) == 0

    def test_destroy_nonexistent_returns_false(self, tmp_home):
        """Destroying a nonexistent deployment should return False."""
        engine = TeamEngine(home=tmp_home)
        assert engine.destroy_deployment("fake-id") is False

    def test_multi_instance_agents(self, tmp_home):
        """Agents with count > 1 should spawn multiple instances."""
        bp = BlueprintManifest(**_minimal_blueprint(
            agents={
                "scout": {
                    "role": "researcher",
                    "model": "fast",
                    "count": 3,
                },
            }
        ))

        engine = TeamEngine(home=tmp_home)
        deployment = engine.deploy(bp)
        assert len(deployment.agents) == 3
        names = list(deployment.agents.keys())
        assert "test-team-scout-1" in names
        assert "test-team-scout-2" in names
        assert "test-team-scout-3" in names


# ---------------------------------------------------------------------------
# Local Provider tests
# ---------------------------------------------------------------------------


class TestLocalProvider:
    """Test the local process provider."""

    def test_provision_creates_directory(self, tmp_home):
        """Provision should create agent working directory."""
        provider = LocalProvider(home=tmp_home)
        spec = AgentSpec(role=AgentRole.WORKER, model=ModelTier.FAST)

        result = provider.provision("test-agent", spec, "test-team")
        assert result["host"] == "localhost"

        work_dir = Path(result["work_dir"])
        assert work_dir.exists()
        assert (work_dir / "config.json").exists()
        assert (work_dir / "memory").is_dir()
        assert (work_dir / "scratch").is_dir()

    def test_destroy_removes_directory(self, tmp_home):
        """Destroy should remove the agent directory."""
        provider = LocalProvider(home=tmp_home)
        spec = AgentSpec()

        result = provider.provision("doomed-agent", spec, "test-team")
        work_dir = Path(result["work_dir"])
        assert work_dir.exists()

        provider.destroy("doomed-agent", result)
        assert not work_dir.exists()

    def test_health_check_running(self, tmp_home):
        """Health check should detect current process as running."""
        import os

        provider = LocalProvider(home=tmp_home)
        result = {"pid": os.getpid()}
        status = provider.health_check("test", result)
        assert status == AgentStatus.RUNNING

    def test_health_check_stopped(self, tmp_home):
        """Health check should detect dead PID as stopped."""
        provider = LocalProvider(home=tmp_home)
        result = {"pid": 99999999}
        status = provider.health_check("test", result)
        assert status == AgentStatus.STOPPED

    def test_health_check_no_pid(self, tmp_home):
        """No PID means stopped."""
        provider = LocalProvider(home=tmp_home)
        status = provider.health_check("test", {})
        assert status == AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# Builtin blueprint validation
# ---------------------------------------------------------------------------


class TestBuiltinBlueprints:
    """Ensure all shipped blueprints are valid."""

    def test_all_builtins_load(self):
        """Every YAML in builtins/ should parse as a valid BlueprintManifest."""
        registry = BlueprintRegistry()
        blueprints = registry.list_blueprints()

        for bp in blueprints:
            assert bp.name, f"{bp.slug} has no name"
            assert bp.description, f"{bp.slug} has no description"
            assert bp.agent_count > 0, f"{bp.slug} has no agents"
            assert bp.slug, f"Blueprint has empty slug"

    def test_no_circular_deps_in_builtins(self):
        """Built-in blueprints should have no circular dependencies."""
        registry = BlueprintRegistry()
        for bp in registry.list_blueprints():
            waves = TeamEngine.resolve_deploy_order(bp)
            assert len(waves) > 0, f"{bp.slug} has empty deploy order"

    def test_infrastructure_guardian_details(self):
        """Spot-check the Infrastructure Guardian blueprint."""
        registry = BlueprintRegistry()
        bp = registry.get("infrastructure-guardian")
        assert bp is not None
        assert "sentinel" in bp.agents
        assert "rook" in bp.agents
        assert bp.coordination.queen == "lumina"
        assert bp.coordination.pattern == "supervisor"

    def test_dev_squadron_details(self):
        """Spot-check the Dev Squadron blueprint."""
        registry = BlueprintRegistry()
        bp = registry.get("dev-squadron")
        assert bp is not None
        assert "architect" in bp.agents
        assert "reviewer" in bp.agents
        assert bp.coordination.pattern == "hierarchical"
