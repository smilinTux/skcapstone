"""Tests for Trustee Operations — restart, scale, rotate, health, logs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

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
from skcapstone.trustee_ops import TrusteeOps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_blueprint(
    agents: dict[str, dict] | None = None,
) -> BlueprintManifest:
    """Create a minimal BlueprintManifest."""
    if agents is None:
        agents = {
            "leader": {"role": "manager", "model": "reason"},
            "worker": {"role": "worker", "model": "fast"},
        }
    specs = {}
    for key, kwargs in agents.items():
        specs[key] = AgentSpec(**kwargs)
    return BlueprintManifest(
        name="test-team",
        slug="test-team",
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
        return {"host": "localhost", "pid": 1234}

    def configure(self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("configure", agent_name))
        return True

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("start", agent_name))
        if agent_name in self.fail_on:
            raise RuntimeError(f"start failed for {agent_name}")
        return True

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("stop", agent_name))
        if agent_name in self.fail_on:
            raise RuntimeError(f"stop failed for {agent_name}")
        return True

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        self.calls.append(("destroy", agent_name))
        return True

    def health_check(self, agent_name: str, provision_result: Dict[str, Any]) -> AgentStatus:
        self.calls.append(("health_check", agent_name))
        if agent_name in self.fail_on:
            return AgentStatus.FAILED
        return AgentStatus.RUNNING


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Create a minimal agent home directory."""
    (tmp_path / "deployments").mkdir()
    (tmp_path / "comms").mkdir()
    (tmp_path / "trustee").mkdir()
    return tmp_path


@pytest.fixture
def provider() -> MockProvider:
    """Create a mock provider."""
    return MockProvider()


@pytest.fixture
def engine(home: Path, provider: MockProvider) -> TeamEngine:
    """Create a TeamEngine with mock provider."""
    return TeamEngine(home=home, provider=provider, comms_root=home / "comms")


@pytest.fixture
def ops(engine: TeamEngine, home: Path) -> TrusteeOps:
    """Create TrusteeOps instance."""
    return TrusteeOps(engine=engine, home=home)


@pytest.fixture
def deployment(engine: TeamEngine) -> TeamDeployment:
    """Deploy a basic team and return the deployment."""
    bp = _make_blueprint()
    return engine.deploy(bp)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


class TestInitialization:
    """Tests for TrusteeOps setup."""

    def test_create_with_engine(self, engine: TeamEngine, home: Path) -> None:
        """TrusteeOps wraps a TeamEngine instance."""
        ops = TrusteeOps(engine=engine, home=home)
        assert ops._engine is engine

    def test_default_home(self, engine: TeamEngine) -> None:
        """TrusteeOps defaults to ~/.skcapstone."""
        ops = TrusteeOps(engine=engine)
        assert str(ops._home).endswith(".skcapstone")


# ---------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------


class TestRestart:
    """Tests for agent restart operations."""

    def test_restart_single_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment, provider: MockProvider,
    ) -> None:
        """Restart a single agent."""
        provider.calls.clear()
        agent_name = list(deployment.agents.keys())[0]
        results = ops.restart_agent(deployment.deployment_id, agent_name=agent_name)
        assert results[agent_name] == "restarted"
        actions = [action for action, _ in provider.calls]
        assert "stop" in actions
        assert "start" in actions

    def test_restart_all_agents(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Restart all agents when no name specified."""
        results = ops.restart_agent(deployment.deployment_id)
        assert all(v == "restarted" for v in results.values())
        assert len(results) == len(deployment.agents)

    def test_restart_nonexistent_deployment(self, ops: TrusteeOps) -> None:
        """Restarting nonexistent deployment raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            ops.restart_agent("ghost")

    def test_restart_nonexistent_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Restarting nonexistent agent raises ValueError."""
        with pytest.raises(ValueError, match="not in deployment"):
            ops.restart_agent(deployment.deployment_id, agent_name="ghost")

    def test_restart_handles_failure(
        self, ops: TrusteeOps, deployment: TeamDeployment, provider: MockProvider,
    ) -> None:
        """Restart reports errors for failed agents."""
        agent_name = list(deployment.agents.keys())[0]
        provider.fail_on.add(agent_name)
        results = ops.restart_agent(deployment.deployment_id, agent_name=agent_name)
        assert "error" in results[agent_name]

    def test_restart_writes_audit(
        self, ops: TrusteeOps, deployment: TeamDeployment, home: Path,
    ) -> None:
        """Restart writes an audit entry."""
        ops.restart_agent(deployment.deployment_id)
        audit_log = home / "coordination" / "audit.log"
        assert audit_log.exists()
        lines = audit_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) >= 1
        import json
        entry = json.loads(lines[-1])
        assert entry["action"] == "restart_agent"


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------


class TestScale:
    """Tests for agent scaling operations."""

    def test_scale_up(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Scale up adds new agent instances."""
        spec_key = list(deployment.agents.values())[0].agent_spec_key
        result = ops.scale_agent(deployment.deployment_id, spec_key, count=3)
        assert "added" in result
        assert result["current_count"] >= 1

    def test_scale_nonexistent_deployment(self, ops: TrusteeOps) -> None:
        """Scaling nonexistent deployment raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            ops.scale_agent("ghost", "worker", count=2)

    def test_scale_invalid_count(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Scale with count < 1 raises ValueError."""
        spec_key = list(deployment.agents.values())[0].agent_spec_key
        with pytest.raises(ValueError):
            ops.scale_agent(deployment.deployment_id, spec_key, count=0)


# ---------------------------------------------------------------------------
# Rotate
# ---------------------------------------------------------------------------


class TestRotate:
    """Tests for agent rotation operations."""

    def test_rotate_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Rotate snapshots and redeploys an agent."""
        agent_name = list(deployment.agents.keys())[0]
        result = ops.rotate_agent(deployment.deployment_id, agent_name)
        assert "snapshot_path" in result

    def test_rotate_nonexistent_deployment(self, ops: TrusteeOps) -> None:
        """Rotating in nonexistent deployment raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            ops.rotate_agent("ghost", "agent")

    def test_rotate_nonexistent_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Rotating nonexistent agent raises ValueError."""
        with pytest.raises(ValueError, match="not"):
            ops.rotate_agent(deployment.deployment_id, "ghost")


# ---------------------------------------------------------------------------
# Health report
# ---------------------------------------------------------------------------


class TestHealthReport:
    """Tests for health report operations."""

    def test_health_report_all_agents(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Health report covers all agents."""
        report = ops.health_report(deployment.deployment_id)
        assert len(report) == len(deployment.agents)

    def test_health_report_structure(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Health report has expected fields."""
        report = ops.health_report(deployment.deployment_id)
        for entry in report:
            assert "name" in entry
            assert "status" in entry
            assert "healthy" in entry

    def test_health_report_calls_provider(
        self, ops: TrusteeOps, deployment: TeamDeployment, provider: MockProvider,
    ) -> None:
        """Health report uses provider.health_check."""
        provider.calls.clear()
        ops.health_report(deployment.deployment_id)
        actions = [action for action, _ in provider.calls]
        assert "health_check" in actions

    def test_health_report_nonexistent(self, ops: TrusteeOps) -> None:
        """Health on nonexistent deployment raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            ops.health_report("ghost")

    def test_health_report_detects_failure(
        self, ops: TrusteeOps, deployment: TeamDeployment, provider: MockProvider,
    ) -> None:
        """Health report detects failed agents."""
        agent_name = list(deployment.agents.keys())[0]
        provider.fail_on.add(agent_name)
        report = ops.health_report(deployment.deployment_id)
        failed = [r for r in report if r["name"] == agent_name]
        assert len(failed) == 1
        assert failed[0]["healthy"] is False


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------


class TestLogs:
    """Tests for log retrieval."""

    def test_get_logs_returns_dict(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """get_logs returns a dict mapping agent names to log lists."""
        logs = ops.get_logs(deployment.deployment_id)
        assert isinstance(logs, dict)

    def test_get_logs_specific_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """get_logs can filter to specific agent."""
        agent_name = list(deployment.agents.keys())[0]
        logs = ops.get_logs(deployment.deployment_id, agent_name=agent_name)
        assert agent_name in logs

    def test_get_logs_nonexistent_deployment(self, ops: TrusteeOps) -> None:
        """Logs for nonexistent deployment raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            ops.get_logs("ghost")

    def test_get_logs_nonexistent_agent(
        self, ops: TrusteeOps, deployment: TeamDeployment,
    ) -> None:
        """Logs for nonexistent agent raises ValueError."""
        with pytest.raises(ValueError):
            ops.get_logs(deployment.deployment_id, agent_name="ghost")
