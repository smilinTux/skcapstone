"""Tests for the Team Engine — deployment orchestration for agent teams."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from skcapstone.blueprints.schema import (
    AgentRole,
    AgentSpec,
    BlueprintManifest,
    ProviderType,
)
from skcapstone.team_engine import (
    AgentStatus,
    DeployedAgent,
    ProviderBackend,
    TeamDeployment,
    TeamEngine,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_blueprint(
    agents: dict[str, dict] | None = None,
    name: str = "test-team",
) -> BlueprintManifest:
    """Create a minimal BlueprintManifest for testing."""
    if agents is None:
        agents = {
            "leader": {"role": "manager", "model": "reason"},
            "worker": {"role": "worker", "model": "fast"},
        }
    specs = {}
    for key, kwargs in agents.items():
        specs[key] = AgentSpec(**kwargs)
    return BlueprintManifest(
        name=name,
        slug=name,
        version="1.0",
        description="Test blueprint",
        agents=specs,
    )


class MockProvider(ProviderBackend):
    """Mock provider that tracks calls."""

    provider_type = ProviderType.LOCAL

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_on: set[str] = set()

    def provision(self, agent_name: str, spec: AgentSpec, team_name: str) -> Dict[str, Any]:
        self.calls.append(("provision", agent_name))
        if agent_name in self.fail_on:
            raise RuntimeError(f"provision failed for {agent_name}")
        return {"host": "localhost", "pid": 1234}

    def configure(self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("configure", agent_name))
        if agent_name in self.fail_on:
            raise RuntimeError(f"configure failed for {agent_name}")
        return True

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("start", agent_name))
        if agent_name in self.fail_on:
            raise RuntimeError(f"start failed for {agent_name}")
        return True

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("stop", agent_name))
        return True

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("destroy", agent_name))
        return True

    def health_check(self, agent_name: str, provision_result: Dict[str, Any]) -> AgentStatus:
        self.calls.append(("health_check", agent_name))
        return AgentStatus.RUNNING


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home directory."""
    (tmp_path / "deployments").mkdir()
    (tmp_path / "comms").mkdir()
    return tmp_path


@pytest.fixture
def provider() -> MockProvider:
    """Create a mock provider backend."""
    return MockProvider()


@pytest.fixture
def engine(home: Path, provider: MockProvider) -> TeamEngine:
    """Create a TeamEngine with mock provider."""
    return TeamEngine(home=home, provider=provider, comms_root=home / "comms")


@pytest.fixture
def blueprint() -> BlueprintManifest:
    """Create a basic two-agent blueprint."""
    return _make_blueprint()


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestModels:
    """Tests for team engine data models."""

    def test_agent_status_values(self) -> None:
        """AgentStatus has expected values."""
        assert AgentStatus.PENDING.value == "pending"
        assert AgentStatus.RUNNING.value == "running"
        assert AgentStatus.FAILED.value == "failed"

    def test_deployed_agent_defaults(self) -> None:
        """DeployedAgent has sensible defaults."""
        agent = DeployedAgent(
            name="test",
            instance_id="inst-1",
            blueprint_slug="test-bp",
            agent_spec_key="worker",
        )
        assert agent.status == AgentStatus.PENDING
        assert agent.host is None
        assert agent.error is None

    def test_team_deployment_defaults(self) -> None:
        """TeamDeployment has sensible defaults."""
        dep = TeamDeployment(
            deployment_id="dep-1",
            blueprint_slug="test-bp",
            team_name="test-team",
            provider=ProviderType.LOCAL,
        )
        assert dep.status == "deploying"
        assert dep.agents == {}

    def test_team_deployment_serialization(self) -> None:
        """TeamDeployment serializes to/from JSON."""
        dep = TeamDeployment(
            deployment_id="dep-1",
            blueprint_slug="test-bp",
            team_name="test-team",
            provider=ProviderType.LOCAL,
            agents={
                "agent-a": DeployedAgent(
                    name="agent-a",
                    instance_id="i-1",
                    blueprint_slug="test-bp",
                    agent_spec_key="worker",
                ),
            },
        )
        data = dep.model_dump_json()
        restored = TeamDeployment.model_validate_json(data)
        assert restored.deployment_id == "dep-1"
        assert "agent-a" in restored.agents


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for TeamEngine setup."""

    def test_engine_creates_deployments_dir(self, tmp_path: Path) -> None:
        """TeamEngine uses given home directory."""
        engine = TeamEngine(home=tmp_path, provider=None)
        assert engine._deployments_dir == tmp_path / "deployments"

    def test_engine_without_provider(self, home: Path) -> None:
        """TeamEngine can be created without a provider (dry-run)."""
        engine = TeamEngine(home=home, provider=None)
        assert engine._provider is None


# ---------------------------------------------------------------------------
# Deploy order resolution
# ---------------------------------------------------------------------------


class TestDeployOrder:
    """Tests for dependency-ordered deployment waves."""

    def test_no_dependencies(self) -> None:
        """Agents without dependencies deploy in one wave."""
        bp = _make_blueprint({
            "a": {"role": "worker", "model": "fast"},
            "b": {"role": "worker", "model": "fast"},
        })
        waves = TeamEngine.resolve_deploy_order(bp)
        assert len(waves) == 1
        assert set(waves[0]) == {"a", "b"}

    def test_simple_dependency(self) -> None:
        """Agent with dependency deploys after its dependency."""
        bp = _make_blueprint({
            "leader": {"role": "manager", "model": "reason"},
            "worker": {"role": "worker", "model": "fast", "depends_on": ["leader"]},
        })
        waves = TeamEngine.resolve_deploy_order(bp)
        assert len(waves) == 2
        assert "leader" in waves[0]
        assert "worker" in waves[1]

    def test_diamond_dependency(self) -> None:
        """Diamond dependency graph resolves correctly."""
        bp = _make_blueprint({
            "base": {"role": "manager", "model": "reason"},
            "mid-a": {"role": "worker", "model": "fast", "depends_on": ["base"]},
            "mid-b": {"role": "worker", "model": "fast", "depends_on": ["base"]},
            "top": {"role": "worker", "model": "fast", "depends_on": ["mid-a", "mid-b"]},
        })
        waves = TeamEngine.resolve_deploy_order(bp)
        assert len(waves) == 3
        assert "base" in waves[0]
        assert set(waves[1]) == {"mid-a", "mid-b"}
        assert "top" in waves[2]


# ---------------------------------------------------------------------------
# Deployment
# ---------------------------------------------------------------------------


class TestDeploy:
    """Tests for deployment orchestration."""

    def test_deploy_creates_agents(
        self, engine: TeamEngine, blueprint: BlueprintManifest,
    ) -> None:
        """Deploy creates agents for each spec in blueprint."""
        dep = engine.deploy(blueprint)
        assert len(dep.agents) == 2
        assert "leader" in dep.agents or any(
            "leader" in a.agent_spec_key for a in dep.agents.values()
        )

    def test_deploy_calls_provider(
        self, engine: TeamEngine, provider: MockProvider, blueprint: BlueprintManifest,
    ) -> None:
        """Deploy calls provision/configure/start on provider."""
        engine.deploy(blueprint)
        actions = [action for action, _ in provider.calls]
        assert "provision" in actions
        assert "configure" in actions
        assert "start" in actions

    def test_deploy_saves_state(
        self, engine: TeamEngine, blueprint: BlueprintManifest, home: Path,
    ) -> None:
        """Deploy persists state to disk."""
        dep = engine.deploy(blueprint)
        state_file = home / "deployments" / f"{dep.deployment_id}.json"
        assert state_file.exists()

    def test_deploy_returns_deployment(
        self, engine: TeamEngine, blueprint: BlueprintManifest,
    ) -> None:
        """Deploy returns a TeamDeployment."""
        dep = engine.deploy(blueprint)
        assert isinstance(dep, TeamDeployment)
        assert dep.blueprint_slug == "test-team"

    def test_deploy_custom_name(
        self, engine: TeamEngine, blueprint: BlueprintManifest,
    ) -> None:
        """Deploy accepts custom deployment name."""
        dep = engine.deploy(blueprint, name="my-team")
        assert dep.team_name == "my-team"

    def test_deploy_without_provider(
        self, home: Path, blueprint: BlueprintManifest,
    ) -> None:
        """Deploy works in dry-run mode without a provider."""
        engine = TeamEngine(home=home, provider=None)
        dep = engine.deploy(blueprint)
        # All agents should be in pending or some initial state
        assert len(dep.agents) >= 1

    def test_deploy_handles_agent_failure(
        self, engine: TeamEngine, provider: MockProvider,
    ) -> None:
        """Deploy continues even if one agent fails."""
        provider.fail_on.add("worker")
        bp = _make_blueprint({
            "leader": {"role": "manager", "model": "reason"},
            "worker": {"role": "worker", "model": "fast"},
        })
        dep = engine.deploy(bp)
        # Deployment should still complete
        assert len(dep.agents) >= 1


# ---------------------------------------------------------------------------
# List / Get / Destroy
# ---------------------------------------------------------------------------


class TestLifecycle:
    """Tests for listing, getting, and destroying deployments."""

    def test_list_empty(self, engine: TeamEngine) -> None:
        """Empty engine returns no deployments."""
        assert engine.list_deployments() == []

    def test_list_after_deploy(
        self, engine: TeamEngine, blueprint: BlueprintManifest,
    ) -> None:
        """Deployed teams appear in listing."""
        engine.deploy(blueprint)
        deps = engine.list_deployments()
        assert len(deps) == 1

    def test_get_deployment(
        self, engine: TeamEngine, blueprint: BlueprintManifest,
    ) -> None:
        """Can retrieve a deployment by ID."""
        dep = engine.deploy(blueprint)
        retrieved = engine.get_deployment(dep.deployment_id)
        assert retrieved is not None
        assert retrieved.deployment_id == dep.deployment_id

    def test_get_nonexistent(self, engine: TeamEngine) -> None:
        """Getting nonexistent deployment returns None."""
        assert engine.get_deployment("ghost") is None

    def test_destroy_deployment(
        self, engine: TeamEngine, provider: MockProvider, blueprint: BlueprintManifest,
    ) -> None:
        """Destroy removes deployment state."""
        dep = engine.deploy(blueprint)
        result = engine.destroy_deployment(dep.deployment_id)
        assert result is True
        assert engine.get_deployment(dep.deployment_id) is None

    def test_destroy_calls_provider(
        self, engine: TeamEngine, provider: MockProvider, blueprint: BlueprintManifest,
    ) -> None:
        """Destroy calls provider.destroy on agents."""
        dep = engine.deploy(blueprint)
        provider.calls.clear()
        engine.destroy_deployment(dep.deployment_id)
        actions = [action for action, _ in provider.calls]
        assert "destroy" in actions

    def test_destroy_nonexistent(self, engine: TeamEngine) -> None:
        """Destroying nonexistent deployment returns False."""
        assert engine.destroy_deployment("ghost") is False

    def test_multiple_deployments(self, engine: TeamEngine) -> None:
        """Can have multiple active deployments."""
        bp1 = _make_blueprint(name="team-alpha")
        bp2 = _make_blueprint(name="team-bravo")
        dep1 = engine.deploy(bp1)
        dep2 = engine.deploy(bp2)
        deps = engine.list_deployments()
        assert len(deps) == 2
        ids = {d.deployment_id for d in deps}
        assert dep1.deployment_id in ids
        assert dep2.deployment_id in ids


# ---------------------------------------------------------------------------
# Provider backend
# ---------------------------------------------------------------------------


class TestProviderBackend:
    """Tests for the abstract provider interface."""

    def test_abstract_methods_raise(self) -> None:
        """Abstract methods raise NotImplementedError."""
        provider = ProviderBackend()
        spec = MagicMock()
        with pytest.raises(NotImplementedError):
            provider.provision("test", spec, "team")
        with pytest.raises(NotImplementedError):
            provider.configure("test", spec, {})
        with pytest.raises(NotImplementedError):
            provider.start("test", {})
        with pytest.raises(NotImplementedError):
            provider.stop("test", {})
        with pytest.raises(NotImplementedError):
            provider.destroy("test", {})
        with pytest.raises(NotImplementedError):
            provider.health_check("test", {})
