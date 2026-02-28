"""
Docker Provider — deploy agent teams as Docker containers.

Each agent runs in its own container with resource limits derived from
the blueprint ResourceSpec. Supports both individual container management
and docker-compose generation for full team orchestration.

The provider wires three sovereign infrastructure components into every
agent container:

1. **Soul Blueprint** — injected via SOUL_BLUEPRINT env and config.json
2. **MCP Server** — host-side skcapstone MCP reachable via env
   (SKCAPSTONE_MCP_HOST / SKCAPSTONE_MCP_SOCKET).  Set one of these so
   containers can call memory_store, coord_claim, etc.
3. **SKComm Transport** — comms directory bind-mounted at /skcomm so
   containers share the same file-channel inboxes as local agents.

Prerequisites:
- Docker daemon running and accessible (DOCKER_HOST or default socket)
- docker Python SDK: pip install docker
- Optional: DOCKER_BASE_IMAGE env var to override the default image
- Optional: SKCOMM_HOME env var for the comms directory
- Optional: SKCAPSTONE_MCP_HOST env var (host:port) or
            SKCAPSTONE_MCP_SOCKET env var (unix socket path)
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from ..blueprints.schema import AgentSpec, BlueprintManifest, ProviderType
from ..team_engine import AgentStatus, ProviderBackend

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "python:3.12-slim"
_GRACEFUL_STOP_TIMEOUT = 15  # seconds before SIGKILL
_MCP_CONTAINER_SOCKET = "/run/skcapstone/mcp.sock"  # path inside containers


def _parse_memory_bytes(mem_str: str) -> int:
    """Convert memory string like '2g' or '512m' to bytes.

    Args:
        mem_str: Memory string with unit suffix (g/G for gigabytes, m/M for
            megabytes).

    Returns:
        Memory in bytes as an integer.
    """
    mem_str = mem_str.strip().lower()
    if mem_str.endswith("g"):
        return int(float(mem_str[:-1]) * 1024 * 1024 * 1024)
    if mem_str.endswith("m"):
        return int(float(mem_str[:-1]) * 1024 * 1024)
    return int(mem_str)


def _nano_cpus(cores: int) -> int:
    """Convert CPU core count to Docker nano_cpus value.

    Args:
        cores: Number of CPU cores.

    Returns:
        NanoCPUs value (cores * 1e9).
    """
    return cores * 1_000_000_000


class DockerProvider(ProviderBackend):
    """Deploy agent teams as Docker containers.

    Each agent spec maps to one (or more) containers with resource limits,
    environment variables, and a mounted config volume. The provider also
    supports generating a docker-compose.yml for full team orchestration.

    Sovereign infrastructure wiring
    --------------------------------
    - **SKComm**: pass ``skcomm_home`` (or set SKCOMM_HOME) to bind-mount
      the comms directory at ``/skcomm`` inside every container so
      container agents share the same file-channel inboxes.
    - **MCP server**: pass ``mcp_host`` (host:port) or ``mcp_socket_path``
      to inject the skcapstone MCP endpoint into container env. Containers
      can then call skcapstone memory, coordination, and heartbeat tools.

    Args:
        base_image: Default Docker image for agent containers.
        network_name: Docker network to attach containers to.
        volume_prefix: Prefix for named volumes created per agent.
        docker_host: Docker daemon socket/URL (default: DOCKER_HOST or
            ``unix:///var/run/docker.sock``).
        skcomm_home: Host-side SKComm comms root directory; bind-mounted at
            ``/skcomm`` inside containers. Reads SKCOMM_HOME if not set.
        mcp_host: Host:port of the skcapstone MCP server (e.g.
            ``"host-gateway:8765"``). Sets SKCAPSTONE_MCP_HOST inside
            containers. Reads SKCAPSTONE_MCP_HOST env if not set.
        mcp_socket_path: Host-side Unix socket for the MCP server.  Bind-
            mounted at /run/skcapstone/mcp.sock and sets
            SKCAPSTONE_MCP_SOCKET inside containers.  Reads
            SKCAPSTONE_MCP_SOCKET env if not set.
    """

    provider_type = ProviderType.DOCKER

    def __init__(
        self,
        base_image: Optional[str] = None,
        network_name: str = "skcapstone",
        volume_prefix: str = "skcapstone-agent",
        docker_host: Optional[str] = None,
        skcomm_home: Optional[str] = None,
        mcp_host: Optional[str] = None,
        mcp_socket_path: Optional[str] = None,
    ) -> None:
        self._base_image = (
            base_image
            or os.environ.get("DOCKER_BASE_IMAGE", _DEFAULT_IMAGE)
        )
        self._network_name = network_name
        self._volume_prefix = volume_prefix
        self._docker_host = docker_host or os.environ.get("DOCKER_HOST", "")
        self._skcomm_home = skcomm_home or os.environ.get("SKCOMM_HOME", "")
        self._mcp_host = mcp_host or os.environ.get("SKCAPSTONE_MCP_HOST", "")
        self._mcp_socket_path = (
            mcp_socket_path or os.environ.get("SKCAPSTONE_MCP_SOCKET", "")
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _client(self):
        """Return an authenticated Docker client.

        Returns:
            docker.DockerClient instance.

        Raises:
            RuntimeError: If the docker SDK is not installed or the daemon
                is unreachable.
        """
        try:
            import docker
        except ImportError:
            raise RuntimeError(
                "Docker provider requires 'docker' SDK: pip install docker"
            )

        kwargs: Dict[str, Any] = {}
        if self._docker_host:
            kwargs["base_url"] = self._docker_host

        try:
            client = docker.from_env(**kwargs)
            client.ping()
            return client
        except Exception as exc:
            raise RuntimeError(
                f"Cannot connect to Docker daemon: {exc}"
            ) from exc

    def _ensure_network(self, client) -> None:
        """Create the shared Docker network if it does not exist.

        Args:
            client: docker.DockerClient instance.
        """
        try:
            client.networks.get(self._network_name)
        except Exception:
            client.networks.create(
                self._network_name,
                driver="bridge",
                check_duplicate=True,
            )
            logger.info("Created Docker network: %s", self._network_name)

    def _volume_name(self, agent_name: str) -> str:
        """Derive the named volume for an agent.

        Args:
            agent_name: Agent instance name.

        Returns:
            Docker volume name string.
        """
        safe = agent_name.replace("_", "-").lower()
        return f"{self._volume_prefix}-{safe}"

    def _container_name(self, agent_name: str) -> str:
        """Derive the container name for an agent.

        Args:
            agent_name: Agent instance name.

        Returns:
            Docker container name string.
        """
        return agent_name.replace("_", "-").lower()

    def _build_agent_config(self, agent_name: str, spec: AgentSpec, team_name: str) -> Dict[str, Any]:
        """Build the agent config dict written into the container.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Config dict ready for JSON serialisation.
        """
        return {
            "agent_name": agent_name,
            "team_name": team_name,
            "role": spec.role.value,
            "model": spec.model_name or spec.model.value,
            "skills": spec.skills,
            "soul_blueprint": spec.soul_blueprint,
            "env": spec.env,
        }

    def _build_sovereign_env(self, env_vars: Dict[str, str]) -> None:
        """Inject sovereign infrastructure env vars into the env dict in-place.

        Adds MCP server endpoint and SKComm home when configured so
        container agents can reach the host-side sovereign stack.

        Args:
            env_vars: Environment variable dict to mutate.
        """
        if self._mcp_host:
            env_vars["SKCAPSTONE_MCP_HOST"] = self._mcp_host
        if self._mcp_socket_path:
            env_vars["SKCAPSTONE_MCP_SOCKET"] = _MCP_CONTAINER_SOCKET
        if self._skcomm_home:
            env_vars["SKCOMM_HOME"] = "/skcomm"

    def _build_volumes_config(self, volume_name: str) -> Dict[str, Any]:
        """Build the volumes dict for containers.create().

        Always includes the per-agent named volume at /agent.
        Optionally adds a bind mount for the SKComm comms directory.

        Args:
            volume_name: Named Docker volume for agent state.

        Returns:
            Volumes dict suitable for docker SDK containers.create().
        """
        vols: Dict[str, Any] = {
            volume_name: {"bind": "/agent", "mode": "rw"},
        }
        if self._skcomm_home and Path(self._skcomm_home).exists():
            vols[self._skcomm_home] = {"bind": "/skcomm", "mode": "rw"}
        if self._mcp_socket_path and Path(self._mcp_socket_path).exists():
            vols[self._mcp_socket_path] = {
                "bind": _MCP_CONTAINER_SOCKET,
                "mode": "ro",
            }
        return vols

    # ------------------------------------------------------------------
    # ProviderBackend interface
    # ------------------------------------------------------------------

    def provision(
        self,
        agent_name: str,
        spec: AgentSpec,
        team_name: str,
    ) -> Dict[str, Any]:
        """Create a Docker container for one agent instance.

        The container is created but NOT started here; start() does that.
        Resource limits (CPU, memory) are applied from spec.resources.
        SKComm and MCP sovereign infrastructure are wired in when
        configured on this provider.

        Args:
            agent_name: Unique agent instance name.
            spec: Agent specification including resource requirements.
            team_name: Parent team name.

        Returns:
            Dict with 'container_id', 'container_name', 'host',
            'volume_name', and 'team_name'.

        Raises:
            RuntimeError: If Docker daemon is unreachable or container
                creation fails.
        """
        client = self._client()
        self._ensure_network(client)

        container_name = self._container_name(agent_name)
        volume_name = self._volume_name(agent_name)

        # Remove any stale container with the same name
        try:
            old = client.containers.get(container_name)
            logger.warning("Removing stale container: %s", container_name)
            old.remove(force=True)
        except Exception:
            pass

        # Ensure named volume for agent state persistence
        try:
            client.volumes.get(volume_name)
        except Exception:
            client.volumes.create(volume_name)
            logger.debug("Created volume: %s", volume_name)

        mem_bytes = _parse_memory_bytes(spec.resources.memory)
        nano_cpus = _nano_cpus(spec.resources.cores)

        env_vars: Dict[str, str] = {
            "AGENT_NAME": agent_name,
            "TEAM_NAME": team_name,
            "AGENT_ROLE": spec.role.value,
            "AGENT_MODEL": spec.model_name or spec.model.value,
        }
        if spec.soul_blueprint:
            env_vars["SOUL_BLUEPRINT"] = spec.soul_blueprint
        env_vars.update(spec.env)

        # Wire sovereign infrastructure (MCP + SKComm)
        self._build_sovereign_env(env_vars)

        volumes_config = self._build_volumes_config(volume_name)

        logger.info(
            "Creating container %s (%s, %s RAM, %d cores)",
            container_name,
            self._base_image,
            spec.resources.memory,
            spec.resources.cores,
        )

        container = client.containers.create(
            image=self._base_image,
            name=container_name,
            environment=env_vars,
            volumes=volumes_config,
            network=self._network_name,
            mem_limit=mem_bytes,
            nano_cpus=nano_cpus,
            # Reason: Keep STDIN open so the container does not exit
            # immediately when used with interactive agent runtimes.
            stdin_open=True,
            tty=False,
            labels={
                "managed_by": "skcapstone",
                "team": team_name,
                "agent": agent_name,
                "role": spec.role.value,
            },
            # Restart unless explicitly stopped (resilience for long-lived agents)
            restart_policy={"Name": "unless-stopped"},
        )

        return {
            "container_id": container.id,
            "container_name": container_name,
            "host": container_name,
            "volume_name": volume_name,
            "team_name": team_name,  # stored so configure() can use it
        }

    def configure(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Write agent configuration into the container volume.

        Injects config.json (soul blueprint, skills, model) into the
        /agent directory via docker exec + shell printf.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            provision_result: Output from provision().

        Returns:
            True if configuration was written successfully.
        """
        container_name = provision_result.get("container_name", "")
        if not container_name:
            return False

        client = self._client()
        try:
            container = client.containers.get(container_name)
        except Exception as exc:
            logger.error("Container %s not found: %s", container_name, exc)
            return False

        # Start container temporarily to write config if not already running
        if container.status != "running":
            container.start()

        team_name = provision_result.get("team_name", "")
        config = self._build_agent_config(agent_name, spec, team_name)
        config_json = json.dumps(config, indent=2)

        # Reason: Use exec to write the config file inside the container so
        # no bind-mounted host path is required; the named volume holds state.
        escaped = config_json.replace("'", "'\\''")
        exit_code, output = container.exec_run(
            cmd=["sh", "-c", f"mkdir -p /agent && printf '%s' '{escaped}' > /agent/config.json"],
            demux=False,
        )

        if exit_code != 0:
            logger.warning(
                "Config write exit_code=%d for %s: %s",
                exit_code, agent_name, output,
            )
            return False

        logger.info("Agent config written to container %s", container_name)
        return True

    def start(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Start the agent container.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the container started successfully.
        """
        client = self._client()
        container_name = provision_result.get("container_name", "")
        if not container_name:
            return False

        try:
            container = client.containers.get(container_name)
            container.start()
            container.reload()
            logger.info(
                "Started container %s (id=%s)",
                container_name, container.id[:12],
            )
            return True
        except Exception as exc:
            logger.error("Failed to start %s: %s", container_name, exc)
            return False

    def stop(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Stop the agent container gracefully (SIGTERM then SIGKILL).

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if stopped or already not running.
        """
        client = self._client()
        container_name = provision_result.get("container_name", "")
        if not container_name:
            return True

        try:
            container = client.containers.get(container_name)
            container.stop(timeout=_GRACEFUL_STOP_TIMEOUT)
            logger.info("Stopped container %s", container_name)
            return True
        except Exception as exc:
            logger.warning("Could not stop %s: %s", container_name, exc)
            return False

    def destroy(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Remove the container and its associated named volume.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if destroyed (container and volume removed).
        """
        self.stop(agent_name, provision_result)

        client = self._client()
        container_name = provision_result.get("container_name", "")
        volume_name = provision_result.get("volume_name", "")

        destroyed = True

        if container_name:
            try:
                container = client.containers.get(container_name)
                container.remove(v=True, force=True)
                logger.info("Removed container %s", container_name)
            except Exception as exc:
                logger.warning("Could not remove container %s: %s", container_name, exc)
                destroyed = False

        if volume_name:
            try:
                vol = client.volumes.get(volume_name)
                vol.remove(force=True)
                logger.info("Removed volume %s", volume_name)
            except Exception as exc:
                logger.debug("Volume %s already removed or missing: %s", volume_name, exc)

        return destroyed

    def rotate(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Destroy the container and redeploy fresh (rotation).

        Used when an agent shows context degradation. Stops and removes
        the old container, then provisions and starts a fresh instance
        with the same spec and team membership.

        The original named volume is removed by destroy() so the new
        container starts with a clean /agent directory.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification for the fresh container.
            provision_result: Output from the previous provision() call;
                used to locate the old container and to recover team_name.

        Returns:
            New provision_result dict from the fresh provision() call.
        """
        team_name = provision_result.get("team_name", "")

        self.destroy(agent_name, provision_result)

        new_result = self.provision(agent_name, spec, team_name)
        self.configure(agent_name, spec, new_result)
        self.start(agent_name, new_result)

        logger.info(
            "Rotated agent %s (old_container=%s new_container=%s)",
            agent_name,
            provision_result.get("container_name", "?"),
            new_result.get("container_name", "?"),
        )
        return new_result

    def health_check(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Inspect container status via docker inspect.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            AgentStatus based on container State.Status.
        """
        client = self._client()
        container_name = provision_result.get("container_name", "")
        if not container_name:
            return AgentStatus.STOPPED

        try:
            container = client.containers.get(container_name)
            container.reload()
            state: str = container.status  # running, exited, paused, …
            if state == "running":
                return AgentStatus.RUNNING
            if state in ("exited", "dead"):
                return AgentStatus.STOPPED
            if state == "paused":
                return AgentStatus.DEGRADED
            return AgentStatus.DEGRADED
        except Exception as exc:
            logger.debug("health_check failed for %s: %s", container_name, exc)
            return AgentStatus.FAILED

    # ------------------------------------------------------------------
    # Docker Compose generation
    # ------------------------------------------------------------------

    def generate_compose(
        self,
        blueprint: BlueprintManifest,
        output_path: Optional[Path] = None,
        include_mcp_service: bool = False,
    ) -> str:
        """Generate a docker-compose.yml from a full blueprint manifest.

        Each agent (and each instance when count > 1) becomes a service.
        Resource limits, environment variables, and soul blueprint paths
        are all included.

        When ``skcomm_home`` is configured on the provider the compose
        output includes a named ``skcomm-data`` volume and mounts it at
        ``/skcomm`` in every agent service.

        When ``include_mcp_service=True`` a ``skcapstone-mcp`` sidecar
        service is added; all agent containers receive ``SKCAPSTONE_MCP_HOST``
        pointing at it so they can reach the MCP server.

        Args:
            blueprint: The validated blueprint manifest.
            output_path: If provided, the YAML is written to this path.
            include_mcp_service: Add a skcapstone-mcp service that agents
                connect to via SKCAPSTONE_MCP_HOST.

        Returns:
            The docker-compose YAML string.
        """
        services: Dict[str, Any] = {}
        volumes: Dict[str, Any] = {}

        # Add skcapstone MCP sidecar if requested
        mcp_service_name = "skcapstone-mcp"
        if include_mcp_service:
            services[mcp_service_name] = {
                "image": self._base_image,
                "container_name": mcp_service_name,
                "command": ["skcapstone", "mcp", "--stdio"],
                "environment": {
                    "SKCAPSTONE_HOME": "/agent/skcapstone",
                },
                "volumes": [
                    "skcapstone-mcp-data:/agent/skcapstone",
                ],
                "networks": [self._network_name],
                "restart": "unless-stopped",
                "labels": [
                    "managed_by=skcapstone",
                    f"team={blueprint.name}",
                    "role=mcp-server",
                ],
            }
            volumes["skcapstone-mcp-data"] = {}

        has_skcomm = bool(self._skcomm_home)
        if has_skcomm:
            volumes["skcomm-data"] = {}

        for agent_key, spec in blueprint.agents.items():
            for idx in range(spec.count):
                suffix = f"-{idx + 1}" if spec.count > 1 else ""
                svc_name = f"{blueprint.slug}-{agent_key}{suffix}".replace("_", "-")
                volume_name = self._volume_name(svc_name)

                env: Dict[str, str] = {
                    "AGENT_NAME": svc_name,
                    "TEAM_NAME": blueprint.name,
                    "AGENT_ROLE": spec.role.value,
                    "AGENT_MODEL": spec.model_name or spec.model.value,
                }
                env.update(spec.env)

                if spec.soul_blueprint:
                    env["SOUL_BLUEPRINT"] = spec.soul_blueprint

                # Wire sovereign infra env vars
                if include_mcp_service:
                    env["SKCAPSTONE_MCP_HOST"] = f"{mcp_service_name}:8765"
                elif self._mcp_host:
                    env["SKCAPSTONE_MCP_HOST"] = self._mcp_host

                if has_skcomm:
                    env["SKCOMM_HOME"] = "/skcomm"

                svc_volumes = [f"{volume_name}:/agent"]
                if has_skcomm:
                    svc_volumes.append("skcomm-data:/skcomm")

                deploy_limits: Dict[str, Any] = {
                    "resources": {
                        "limits": {
                            "cpus": str(spec.resources.cores),
                            "memory": spec.resources.memory.upper(),
                        }
                    }
                }

                service: Dict[str, Any] = {
                    "image": self._base_image,
                    "container_name": svc_name,
                    "environment": env,
                    "volumes": svc_volumes,
                    "networks": [self._network_name],
                    "restart": "unless-stopped",
                    "deploy": deploy_limits,
                    "labels": [
                        "managed_by=skcapstone",
                        f"team={blueprint.name}",
                        f"agent={svc_name}",
                        f"role={spec.role.value}",
                    ],
                }

                if spec.depends_on and include_mcp_service:
                    # Always depend on MCP service first when included
                    service["depends_on"] = [mcp_service_name] + [
                        f"{blueprint.slug}-{dep}".replace("_", "-")
                        for dep in spec.depends_on
                    ]
                elif spec.depends_on:
                    service["depends_on"] = [
                        f"{blueprint.slug}-{dep}".replace("_", "-")
                        for dep in spec.depends_on
                    ]
                elif include_mcp_service:
                    service["depends_on"] = [mcp_service_name]

                services[svc_name] = service
                volumes[volume_name] = {}

        compose: Dict[str, Any] = {
            "version": "3.9",
            "services": services,
            "volumes": {k: {} for k in volumes},
            "networks": {
                self._network_name: {
                    "driver": "bridge",
                }
            },
        }

        compose_yaml = yaml.dump(compose, default_flow_style=False, sort_keys=False)

        if output_path:
            Path(output_path).write_text(compose_yaml, encoding="utf-8")
            logger.info("docker-compose.yml written to %s", output_path)

        return compose_yaml
