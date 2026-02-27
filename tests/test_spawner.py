"""Tests for the sub-agent spawner module."""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.blueprints.schema import AgentRole, ModelTier, ProviderType
from skcapstone.spawner import (
    NodeInfo,
    SpawnResult,
    SubAgentSpawner,
    classify_task,
    select_node,
)
from skcapstone.team_engine import AgentStatus


# ---------------------------------------------------------------------------
# classify_task
# ---------------------------------------------------------------------------


class TestClassifyTask:
    """Tests for automatic task classification."""

    def test_coding_task(self):
        role, model = classify_task("Write unit tests for capauth login flow")
        assert role == AgentRole.CODER
        assert model == ModelTier.CODE

    def test_review_task(self):
        role, model = classify_task("Code review the skchat architecture")
        assert role == AgentRole.REVIEWER
        assert model == ModelTier.REASON

    def test_research_task(self):
        role, model = classify_task("Research FUSE mounting options for Linux")
        assert role == AgentRole.RESEARCHER
        assert model == ModelTier.REASON

    def test_docs_task(self):
        role, model = classify_task("Write docs for the spawner module")
        assert role == AgentRole.DOCUMENTARIAN
        assert model == ModelTier.FAST

    def test_security_task(self):
        role, model = classify_task("Run a security audit on the capauth service")
        assert role == AgentRole.SECURITY
        assert model == ModelTier.REASON

    def test_ops_task(self):
        role, model = classify_task("Deploy the monitoring stack to production")
        assert role == AgentRole.OPS
        assert model == ModelTier.FAST

    def test_unknown_falls_back_to_worker(self):
        role, model = classify_task("Do something completely unrecognizable")
        assert role == AgentRole.WORKER
        assert model == ModelTier.FAST

    def test_case_insensitive(self):
        role, model = classify_task("IMPLEMENT the new feature")
        assert role == AgentRole.CODER

    def test_multi_word_pattern_priority(self):
        """Multi-word patterns should match before single-word ones."""
        role, model = classify_task("Conduct a security audit of the system")
        assert role == AgentRole.SECURITY
        assert model == ModelTier.REASON


# ---------------------------------------------------------------------------
# select_node
# ---------------------------------------------------------------------------


class TestSelectNode:
    """Tests for node selection logic."""

    def test_empty_nodes_returns_local(self):
        node = select_node([], AgentRole.CODER, ModelTier.CODE)
        assert node.provider == ProviderType.LOCAL
        assert node.name == "local"

    def test_prefers_provider_match(self):
        nodes = [
            NodeInfo(name="docker1", provider=ProviderType.DOCKER, capacity=0.5),
            NodeInfo(name="local1", provider=ProviderType.LOCAL, capacity=0.9),
        ]
        node = select_node(
            nodes, AgentRole.CODER, ModelTier.CODE,
            preferred_provider=ProviderType.DOCKER,
        )
        assert node.name == "docker1"

    def test_prefers_high_capacity(self):
        nodes = [
            NodeInfo(name="low", provider=ProviderType.LOCAL, capacity=0.2),
            NodeInfo(name="high", provider=ProviderType.LOCAL, capacity=0.9),
        ]
        node = select_node(nodes, AgentRole.WORKER, ModelTier.FAST)
        assert node.name == "high"

    def test_gpu_affinity_for_reason_models(self):
        nodes = [
            NodeInfo(name="cpu", provider=ProviderType.LOCAL, capacity=0.8),
            NodeInfo(name="gpu", provider=ProviderType.LOCAL, capacity=0.5, tags=["gpu"]),
        ]
        node = select_node(nodes, AgentRole.RESEARCHER, ModelTier.REASON)
        assert node.name == "gpu"

    def test_local_affinity_for_local_models(self):
        nodes = [
            NodeInfo(name="docker1", provider=ProviderType.DOCKER, capacity=0.9),
            NodeInfo(name="local1", provider=ProviderType.LOCAL, capacity=0.5),
        ]
        node = select_node(nodes, AgentRole.WORKER, ModelTier.LOCAL)
        assert node.name == "local1"


# ---------------------------------------------------------------------------
# SubAgentSpawner
# ---------------------------------------------------------------------------


class TestSubAgentSpawner:
    """Tests for the spawner's spawn and management methods."""

    def test_spawn_creates_deployment(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        result = spawner.spawn(task="Write tests for capauth")

        assert isinstance(result, SpawnResult)
        assert result.deployment_id != ""
        assert result.role == AgentRole.CODER
        assert result.model == ModelTier.CODE

    def test_spawn_with_explicit_role(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        result = spawner.spawn(
            task="Something generic",
            role=AgentRole.SECURITY,
            model=ModelTier.REASON,
        )
        assert result.role == AgentRole.SECURITY
        assert result.model == ModelTier.REASON

    def test_spawn_creates_deployments_dir(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        spawner.spawn(task="Test deployment creation")

        deployments_dir = tmp_agent_home / "deployments"
        assert deployments_dir.exists()
        assert len(list(deployments_dir.glob("*.json"))) == 1

    def test_list_spawned_empty(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        results = spawner.list_spawned()
        assert results == []

    def test_list_spawned_after_spawn(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        spawner.spawn(task="Test listing")
        results = spawner.list_spawned()
        assert len(results) == 1

    def test_kill_destroys_deployment(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        result = spawner.spawn(task="Test killing")
        assert spawner.kill(result.deployment_id)

        # Should be gone now
        results = spawner.list_spawned()
        assert len(results) == 0

    def test_kill_nonexistent_returns_false(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        assert not spawner.kill("nonexistent-deployment-id")

    def test_spawn_batch(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        tasks = [
            {"task": "Write unit tests"},
            {"task": "Review architecture"},
            {"task": "Write documentation"},
        ]
        results = spawner.spawn_batch(tasks)
        assert len(results) == 3
        assert results[0].role == AgentRole.CODER
        assert results[1].role == AgentRole.REVIEWER
        assert results[2].role == AgentRole.DOCUMENTARIAN

    def test_spawn_with_custom_name(self, tmp_agent_home: Path):
        spawner = SubAgentSpawner(home=tmp_agent_home)
        result = spawner.spawn(
            task="Custom named agent",
            agent_name="my-custom-agent",
        )
        assert result.deployment_id != ""

    def test_spawn_writes_audit(self, tmp_agent_home: Path):
        (tmp_agent_home / "coordination").mkdir(parents=True, exist_ok=True)
        spawner = SubAgentSpawner(home=tmp_agent_home)
        spawner.spawn(task="Audit test task")

        audit_path = tmp_agent_home / "coordination" / "audit.log"
        assert audit_path.exists()
        content = audit_path.read_text()
        assert "spawn_agent" in content
