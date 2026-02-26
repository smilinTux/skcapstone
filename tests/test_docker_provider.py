"""Tests for the Docker provider backend.

All Docker SDK calls are mocked so no real daemon is required.
Covers provision, configure, start, stop, destroy, health_check,
and generate_compose.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from skcapstone.blueprints.schema import (
    AgentRole,
    AgentSpec,
    BlueprintManifest,
    ModelTier,
    ProviderType,
    ResourceSpec,
)
from skcapstone.providers.docker import (
    DockerProvider,
    _DEFAULT_IMAGE,
    _nano_cpus,
    _parse_memory_bytes,
)
from skcapstone.team_engine import AgentStatus


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    role: str = "worker",
    model: str = "fast",
    memory: str = "2g",
    cores: int = 1,
    skills: list | None = None,
    soul_blueprint: str | None = None,
    env: dict | None = None,
) -> AgentSpec:
    """Build a minimal AgentSpec for testing."""
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        resources=ResourceSpec(memory=memory, cores=cores),
        skills=skills or [],
        soul_blueprint=soul_blueprint,
        env=env or {},
    )


def _make_blueprint(agent_count: int = 1) -> BlueprintManifest:
    """Build a minimal BlueprintManifest for testing."""
    agents = {
        f"agent{i}": _make_spec() for i in range(agent_count)
    }
    return BlueprintManifest(
        name="Test Team",
        slug="test-team",
        description="Unit-test blueprint",
        agents=agents,
        default_provider=ProviderType.DOCKER,
    )


def _provision_result(
    container_name: str = "test-agent",
    container_id: str = "abc123def456",
    volume_name: str = "skcapstone-agent-test-agent",
) -> Dict[str, Any]:
    """Return a typical provision_result dict."""
    return {
        "container_id": container_id,
        "container_name": container_name,
        "host": container_name,
        "volume_name": volume_name,
    }


@pytest.fixture()
def provider() -> DockerProvider:
    """Return a DockerProvider with default settings."""
    return DockerProvider(
        base_image="python:3.12-slim",
        network_name="skcapstone",
        volume_prefix="skcapstone-agent",
    )


@pytest.fixture()
def mock_docker_client():
    """Return a MagicMock simulating docker.DockerClient."""
    client = MagicMock()
    client.ping.return_value = True

    # Simulate network not existing initially
    client.networks.get.side_effect = Exception("not found")

    # Simulate containers.get raising when looking for stale container
    client.containers.get.side_effect = Exception("not found")

    # Simulate volume not existing
    client.volumes.get.side_effect = Exception("not found")
    client.volumes.create.return_value = MagicMock()

    # Container mock
    mock_container = MagicMock()
    mock_container.id = "abc123def456"
    mock_container.status = "created"
    client.containers.create.return_value = mock_container

    return client, mock_container


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------


class TestParseMemoryBytes:
    """Tests for _parse_memory_bytes helper."""

    def test_gigabytes(self):
        assert _parse_memory_bytes("2g") == 2 * 1024 ** 3

    def test_megabytes(self):
        assert _parse_memory_bytes("512m") == 512 * 1024 ** 2

    def test_uppercase_suffix(self):
        assert _parse_memory_bytes("1G") == 1 * 1024 ** 3

    def test_numeric_only(self):
        assert _parse_memory_bytes("1073741824") == 1073741824

    def test_fractional_gigabytes(self):
        assert _parse_memory_bytes("0.5g") == int(0.5 * 1024 ** 3)


class TestNanoCpus:
    """Tests for _nano_cpus helper."""

    def test_single_core(self):
        assert _nano_cpus(1) == 1_000_000_000

    def test_four_cores(self):
        assert _nano_cpus(4) == 4_000_000_000


# ---------------------------------------------------------------------------
# DockerProvider._client
# ---------------------------------------------------------------------------


class TestDockerProviderClient:
    """Tests for _client() connection logic."""

    def test_raises_if_sdk_missing(self, provider: DockerProvider):
        with patch.dict("sys.modules", {"docker": None}):
            with pytest.raises(RuntimeError, match="pip install docker"):
                provider._client()

    def test_raises_if_daemon_unreachable(self, provider: DockerProvider):
        mock_docker = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_instance.ping.side_effect = Exception("connection refused")
        mock_docker.from_env.return_value = mock_client_instance

        with patch.dict("sys.modules", {"docker": mock_docker}):
            with pytest.raises(RuntimeError, match="Cannot connect"):
                provider._client()

    def test_returns_client_on_success(self, provider: DockerProvider):
        mock_docker = MagicMock()
        mock_client_instance = MagicMock()
        mock_client_instance.ping.return_value = True
        mock_docker.from_env.return_value = mock_client_instance

        with patch.dict("sys.modules", {"docker": mock_docker}):
            result = provider._client()

        assert result is mock_client_instance


# ---------------------------------------------------------------------------
# provision()
# ---------------------------------------------------------------------------


class TestProvision:
    """Tests for DockerProvider.provision()."""

    def _run_provision(self, provider, mock_client, mock_container):
        spec = _make_spec(memory="1g", cores=2)
        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.provision("my-agent", spec, "my-team")
        return result

    def test_returns_expected_keys(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        result = self._run_provision(provider, mock_client, mock_container)

        assert "container_id" in result
        assert "container_name" in result
        assert "host" in result
        assert "volume_name" in result

    def test_container_name_derived_from_agent_name(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        result = self._run_provision(provider, mock_client, mock_container)
        assert result["container_name"] == "my-agent"

    def test_network_created_if_missing(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        self._run_provision(provider, mock_client, mock_container)
        mock_client.networks.create.assert_called_once()

    def test_network_not_created_if_exists(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        mock_client.networks.get.side_effect = None  # network exists
        mock_client.networks.get.return_value = MagicMock()
        self._run_provision(provider, mock_client, mock_container)
        mock_client.networks.create.assert_not_called()

    def test_volume_created(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        self._run_provision(provider, mock_client, mock_container)
        mock_client.volumes.create.assert_called_once()

    def test_memory_limit_applied(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        spec = _make_spec(memory="2g", cores=1)
        with patch.object(provider, "_client", return_value=mock_client):
            provider.provision("agent-x", spec, "team-y")

        kwargs = mock_client.containers.create.call_args[1]
        assert kwargs["mem_limit"] == 2 * 1024 ** 3

    def test_cpu_limit_applied(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        spec = _make_spec(memory="512m", cores=4)
        with patch.object(provider, "_client", return_value=mock_client):
            provider.provision("agent-x", spec, "team-y")

        kwargs = mock_client.containers.create.call_args[1]
        assert kwargs["nano_cpus"] == 4_000_000_000

    def test_environment_vars_set(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        spec = _make_spec(env={"MY_KEY": "my_value"})
        with patch.object(provider, "_client", return_value=mock_client):
            provider.provision("agent-x", spec, "team-y")

        kwargs = mock_client.containers.create.call_args[1]
        env = kwargs["environment"]
        assert env["AGENT_NAME"] == "agent-x"
        assert env["TEAM_NAME"] == "team-y"
        assert env["MY_KEY"] == "my_value"

    def test_stale_container_removed(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        stale = MagicMock()
        # First call returns stale container; subsequent return nothing
        mock_client.containers.get.side_effect = [stale, Exception("not found")]
        spec = _make_spec()
        with patch.object(provider, "_client", return_value=mock_client):
            provider.provision("my-agent", spec, "team")

        stale.remove.assert_called_once_with(force=True)

    def test_edge_underscores_in_name_normalised(self, provider, mock_docker_client):
        mock_client, mock_container = mock_docker_client
        spec = _make_spec()
        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.provision("my_agent_name", spec, "team")
        assert result["container_name"] == "my-agent-name"


# ---------------------------------------------------------------------------
# configure()
# ---------------------------------------------------------------------------


class TestConfigure:
    """Tests for DockerProvider.configure()."""

    def test_returns_true_on_success(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.exec_run.return_value = (0, b"")
        mock_client.containers.get.return_value = mock_container

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.configure(
                "my-agent",
                _make_spec(),
                _provision_result("my-agent"),
            )

        assert result is True

    def test_starts_stopped_container_before_config(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.status = "created"
        mock_container.exec_run.return_value = (0, b"")
        mock_client.containers.get.return_value = mock_container

        with patch.object(provider, "_client", return_value=mock_client):
            provider.configure("my-agent", _make_spec(), _provision_result("my-agent"))

        mock_container.start.assert_called_once()

    def test_returns_false_if_container_missing(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("not found")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.configure(
                "ghost-agent",
                _make_spec(),
                _provision_result("ghost-agent"),
            )

        assert result is False

    def test_returns_false_if_exec_fails(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.status = "running"
        mock_container.exec_run.return_value = (1, b"error")
        mock_client.containers.get.return_value = mock_container

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.configure(
                "fail-agent",
                _make_spec(),
                _provision_result("fail-agent"),
            )

        assert result is False

    def test_empty_container_name_returns_false(self, provider):
        result = provider.configure("x", _make_spec(), {})
        assert result is False


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------


class TestStart:
    """Tests for DockerProvider.start()."""

    def test_returns_true_on_success(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.id = "abc"
        mock_client.containers.get.return_value = mock_container

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.start("agent", _provision_result())

        assert result is True
        mock_container.start.assert_called_once()

    def test_returns_false_on_docker_error(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("not found")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.start("ghost", _provision_result("ghost"))

        assert result is False

    def test_empty_container_name_returns_false(self, provider):
        mock_client = MagicMock()
        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.start("x", {})
        assert result is False


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------


class TestStop:
    """Tests for DockerProvider.stop()."""

    def test_returns_true_on_success(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.stop("agent", _provision_result())

        assert result is True
        mock_container.stop.assert_called_once_with(timeout=15)

    def test_returns_false_on_docker_error(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = MagicMock()
        mock_client.containers.get.return_value.stop.side_effect = Exception("err")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.stop("agent", _provision_result())

        assert result is False

    def test_empty_container_name_returns_true(self, provider):
        mock_client = MagicMock()
        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.stop("x", {})
        assert result is True


# ---------------------------------------------------------------------------
# destroy()
# ---------------------------------------------------------------------------


class TestDestroy:
    """Tests for DockerProvider.destroy()."""

    def test_removes_container_and_volume(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_volume = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_client.volumes.get.return_value = mock_volume

        pr = _provision_result()
        with patch.object(provider, "_client", return_value=mock_client):
            with patch.object(provider, "stop", return_value=True):
                result = provider.destroy("agent", pr)

        assert result is True
        mock_container.remove.assert_called_once_with(v=True, force=True)
        mock_volume.remove.assert_called_once_with(force=True)

    def test_returns_false_if_container_remove_fails(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.remove.side_effect = Exception("locked")
        mock_client.containers.get.return_value = mock_container
        mock_client.volumes.get.side_effect = Exception("no vol")

        pr = _provision_result()
        with patch.object(provider, "_client", return_value=mock_client):
            with patch.object(provider, "stop", return_value=True):
                result = provider.destroy("agent", pr)

        assert result is False

    def test_tolerates_missing_volume(self, provider):
        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_client.containers.get.return_value = mock_container
        mock_client.volumes.get.side_effect = Exception("not found")

        pr = _provision_result()
        with patch.object(provider, "_client", return_value=mock_client):
            with patch.object(provider, "stop", return_value=True):
                result = provider.destroy("agent", pr)

        assert result is True


# ---------------------------------------------------------------------------
# health_check()
# ---------------------------------------------------------------------------


class TestHealthCheck:
    """Tests for DockerProvider.health_check()."""

    def _make_container(self, status: str) -> MagicMock:
        c = MagicMock()
        c.status = status
        return c

    def test_running_returns_running(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = self._make_container("running")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("agent", _provision_result())

        assert result == AgentStatus.RUNNING

    def test_exited_returns_stopped(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = self._make_container("exited")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("agent", _provision_result())

        assert result == AgentStatus.STOPPED

    def test_paused_returns_degraded(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = self._make_container("paused")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("agent", _provision_result())

        assert result == AgentStatus.DEGRADED

    def test_dead_returns_stopped(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = self._make_container("dead")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("agent", _provision_result())

        assert result == AgentStatus.STOPPED

    def test_unknown_state_returns_degraded(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.return_value = self._make_container("restarting")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("agent", _provision_result())

        assert result == AgentStatus.DEGRADED

    def test_missing_container_returns_failed(self, provider):
        mock_client = MagicMock()
        mock_client.containers.get.side_effect = Exception("not found")

        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("ghost", _provision_result("ghost"))

        assert result == AgentStatus.FAILED

    def test_empty_container_name_returns_stopped(self, provider):
        mock_client = MagicMock()
        with patch.object(provider, "_client", return_value=mock_client):
            result = provider.health_check("x", {})
        assert result == AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# generate_compose()
# ---------------------------------------------------------------------------


class TestGenerateCompose:
    """Tests for DockerProvider.generate_compose()."""

    def test_returns_valid_yaml(self, provider):
        bp = _make_blueprint(agent_count=2)
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert isinstance(parsed, dict)
        assert "services" in parsed

    def test_services_match_agent_count(self, provider):
        bp = _make_blueprint(agent_count=3)
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert len(parsed["services"]) == 3

    def test_volumes_section_present(self, provider):
        bp = _make_blueprint(agent_count=1)
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert "volumes" in parsed

    def test_networks_section_present(self, provider):
        bp = _make_blueprint(agent_count=1)
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert "networks" in parsed
        assert "skcapstone" in parsed["networks"]

    def test_memory_in_deploy_limits(self, provider):
        bp = BlueprintManifest(
            name="Mem Team",
            slug="mem-team",
            description="test",
            agents={"alpha": _make_spec(memory="4g", cores=2)},
            default_provider=ProviderType.DOCKER,
        )
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        svc = list(parsed["services"].values())[0]
        mem = svc["deploy"]["resources"]["limits"]["memory"]
        assert "4G" in mem.upper()

    def test_cpu_in_deploy_limits(self, provider):
        bp = BlueprintManifest(
            name="Cpu Team",
            slug="cpu-team",
            description="test",
            agents={"alpha": _make_spec(cores=4)},
            default_provider=ProviderType.DOCKER,
        )
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        svc = list(parsed["services"].values())[0]
        cpus = svc["deploy"]["resources"]["limits"]["cpus"]
        assert cpus == "4"

    def test_soul_blueprint_in_env_when_set(self, provider):
        bp = BlueprintManifest(
            name="Soul Team",
            slug="soul-team",
            description="test",
            agents={"alpha": _make_spec(soul_blueprint="souls/sentinel.yaml")},
            default_provider=ProviderType.DOCKER,
        )
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        svc = list(parsed["services"].values())[0]
        assert svc["environment"].get("SOUL_BLUEPRINT") == "souls/sentinel.yaml"

    def test_count_expands_to_multiple_services(self, provider):
        spec = AgentSpec(
            role=AgentRole.WORKER,
            model=ModelTier.FAST,
            resources=ResourceSpec(),
            count=3,
        )
        bp = BlueprintManifest(
            name="Scale Team",
            slug="scale-team",
            description="test",
            agents={"worker": spec},
            default_provider=ProviderType.DOCKER,
        )
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert len(parsed["services"]) == 3

    def test_writes_to_file_when_output_path_provided(self, provider, tmp_path):
        bp = _make_blueprint()
        out = tmp_path / "docker-compose.yml"
        provider.generate_compose(bp, output_path=out)
        assert out.exists()
        content = yaml.safe_load(out.read_text())
        assert "services" in content

    def test_edge_empty_agents_produces_no_services(self, provider):
        """Edge case: blueprint with no agents should yield empty services."""
        bp = BlueprintManifest(
            name="Empty Team",
            slug="empty-team",
            description="no agents",
            agents={},
            default_provider=ProviderType.DOCKER,
        )
        output = provider.generate_compose(bp)
        parsed = yaml.safe_load(output)
        assert parsed["services"] == {} or parsed["services"] is None
