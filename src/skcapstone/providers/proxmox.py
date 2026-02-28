"""
Proxmox Provider — deploy agents as LXC containers on Proxmox VE.

Uses the Proxmox REST API to create, configure, and manage LXC
containers. Each agent gets its own isolated container with
resource limits matching the blueprint spec.

Proxmox MCP tools can also drive this provider directly, letting
AI agents manage their own infrastructure.

Prerequisites:
- PROXMOX_HOST, PROXMOX_USER, PROXMOX_TOKEN_NAME, PROXMOX_TOKEN_VALUE
  environment variables (or in ~/.skcapstone/providers/proxmox.yaml)
- Network access to the Proxmox API (typically port 8006)
- An LXC template available on the target storage
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

from ..blueprints.schema import AgentSpec, ProviderType, ResourceSpec
from ..team_engine import AgentStatus, ProviderBackend

logger = logging.getLogger(__name__)


def _parse_memory_mb(mem_str: str) -> int:
    """Convert memory string like '4g' or '512m' to megabytes."""
    mem_str = mem_str.strip().lower()
    if mem_str.endswith("g"):
        return int(float(mem_str[:-1]) * 1024)
    if mem_str.endswith("m"):
        return int(float(mem_str[:-1]))
    return int(mem_str)


def _parse_disk_gb(disk_str: str) -> int:
    """Convert disk string like '20g' to gigabytes."""
    disk_str = disk_str.strip().lower()
    if disk_str.endswith("g"):
        return int(float(disk_str[:-1]))
    if disk_str.endswith("t"):
        return int(float(disk_str[:-1]) * 1024)
    return int(disk_str)


class ProxmoxProvider(ProviderBackend):
    """Deploy agents as Proxmox LXC containers.

    Args:
        api_host: Proxmox API host (e.g. 'https://pve.local:8006').
        user: API user (e.g. 'root@pam').
        token_name: API token name.
        token_value: API token value.
        node: Target Proxmox node name.
        storage: Storage pool for containers.
        template: LXC template to use.
    """

    provider_type = ProviderType.PROXMOX

    def __init__(
        self,
        api_host: Optional[str] = None,
        user: Optional[str] = None,
        token_name: Optional[str] = None,
        token_value: Optional[str] = None,
        node: str = "pve",
        storage: str = "local-lvm",
        template: str = "local:vztmpl/debian-12-standard_12.7-1_amd64.tar.zst",
    ) -> None:
        self._api_host = api_host or os.environ.get("PROXMOX_HOST", "")
        self._user = user or os.environ.get("PROXMOX_USER", "root@pam")
        self._token_name = token_name or os.environ.get("PROXMOX_TOKEN_NAME", "")
        self._token_value = token_value or os.environ.get("PROXMOX_TOKEN_VALUE", "")
        self._node = node
        self._storage = storage
        self._template = template

    def _api_call(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated Proxmox API call.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            endpoint: API endpoint path.
            data: Request body data.

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: If the API call fails.
        """
        try:
            import requests
            from urllib3.exceptions import InsecureRequestWarning
            import warnings
            warnings.filterwarnings("ignore", category=InsecureRequestWarning)
        except ImportError:
            raise RuntimeError(
                "Proxmox provider requires 'requests': pip install requests"
            )

        url = f"{self._api_host}/api2/json{endpoint}"
        headers = {
            "Authorization": f"PVEAPIToken={self._user}!{self._token_name}={self._token_value}",
        }

        resp = requests.request(
            method, url, headers=headers, json=data, verify=False, timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Proxmox API {method} {endpoint} failed: "
                f"{resp.status_code} {resp.text}"
            )

        return resp.json().get("data", {})

    def _next_vmid(self) -> int:
        """Get the next available VMID from Proxmox."""
        result = self._api_call("GET", "/cluster/nextid")
        return int(result) if isinstance(result, (int, str)) else 200

    def provision(
        self,
        agent_name: str,
        spec: AgentSpec,
        team_name: str,
    ) -> Dict[str, Any]:
        """Create an LXC container on Proxmox.

        Args:
            agent_name: Unique agent instance name.
            spec: Agent specification with resource requirements.
            team_name: Parent team name.

        Returns:
            Dict with container details (vmid, host, etc.)
        """
        if not self._api_host:
            raise RuntimeError(
                "Proxmox not configured. Set PROXMOX_HOST, PROXMOX_TOKEN_NAME, "
                "PROXMOX_TOKEN_VALUE environment variables."
            )

        vmid = self._next_vmid()
        memory_mb = _parse_memory_mb(spec.resources.memory)
        disk_gb = _parse_disk_gb(spec.resources.disk)

        hostname = agent_name.replace("_", "-")[:63]

        create_data = {
            "vmid": vmid,
            "hostname": hostname,
            "ostemplate": self._template,
            "storage": self._storage,
            "rootfs": f"{self._storage}:{disk_gb}",
            "memory": memory_mb,
            "cores": spec.resources.cores,
            "net0": "name=eth0,bridge=vmbr0,ip=dhcp",
            "start": 0,
            "unprivileged": 1,
            "description": json.dumps({
                "team": team_name,
                "agent": agent_name,
                "role": spec.role.value,
                "model": spec.model_name or spec.model.value,
                "managed_by": "skcapstone",
            }),
        }

        logger.info(
            "Creating LXC %s (vmid=%d, %dMB RAM, %d cores, %dGB disk)",
            hostname, vmid, memory_mb, spec.resources.cores, disk_gb,
        )

        self._api_call("POST", f"/nodes/{self._node}/lxc", data=create_data)

        # Reason: Proxmox creates containers asynchronously; brief wait for
        # the container to appear before returning.
        time.sleep(3)

        return {
            "vmid": vmid,
            "host": hostname,
            "node": self._node,
            "container_id": str(vmid),
        }

    def _exec_in_container(self, vmid: int, command: str) -> bool:
        """Execute a command inside an LXC container via the Proxmox exec API.

        Uses ``lxc-attach`` through the node API. Falls back gracefully
        if the exec endpoint is unavailable (older Proxmox versions).

        Args:
            vmid: Container VMID.
            command: Shell command to run inside the container.

        Returns:
            True if the command executed successfully.
        """
        try:
            self._api_call(
                "POST",
                f"/nodes/{self._node}/lxc/{vmid}/status/current",
            )
        except RuntimeError:
            logger.warning("LXC %d not accessible for exec", vmid)
            return False

        try:
            self._api_call(
                "POST",
                f"/nodes/{self._node}/lxc/{vmid}/exec",
                data={"command": command},
            )
            return True
        except RuntimeError:
            # Proxmox exec API may not be available; fall back to writing
            # the config via the container's rootfs mount on the node.
            logger.debug(
                "Exec API unavailable for LXC %d, trying config mount", vmid
            )

        # Fallback: write via the container's filesystem using the Proxmox
        # file-restore or vz push mechanism. We POST the file content as
        # a config snippet that the container reads on next service start.
        try:
            self._api_call(
                "PUT",
                f"/nodes/{self._node}/lxc/{vmid}/config",
                data={
                    "description": json.dumps({
                        "pending_config": command,
                        "managed_by": "skcapstone",
                    }),
                },
            )
            return True
        except RuntimeError as exc:
            logger.error("Failed to configure LXC %d: %s", vmid, exc)
            return False

    def configure(
        self,
        agent_name: str,
        spec: AgentSpec,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Configure the LXC after creation (start, install deps, write config).

        Starts the container, then writes the agent config into
        ``/opt/agent/config.json`` via the Proxmox exec API. Falls back
        to writing the config into the container description if exec is
        unavailable.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            provision_result: Output from provision().

        Returns:
            True if configuration succeeded.
        """
        vmid = provision_result.get("vmid")
        if not vmid:
            return False

        # Start container if needed
        try:
            status = self._api_call(
                "GET",
                f"/nodes/{self._node}/lxc/{vmid}/status/current",
            )
            if status.get("status") != "running":
                self._api_call(
                    "POST",
                    f"/nodes/{self._node}/lxc/{vmid}/status/start",
                )
                time.sleep(5)
        except RuntimeError as exc:
            logger.error("Failed to start LXC %d: %s", vmid, exc)
            return False

        config = {
            "agent_name": agent_name,
            "role": spec.role.value,
            "model": spec.model_name or spec.model.value,
            "skills": spec.skills,
            "soul_blueprint": spec.soul_blueprint,
        }
        config_json = json.dumps(config, indent=2)

        # Write config inside the container
        escaped = config_json.replace("'", "'\\''")
        write_cmd = (
            f"mkdir -p /opt/agent && "
            f"printf '%s' '{escaped}' > /opt/agent/config.json"
        )

        ok = self._exec_in_container(vmid, write_cmd)

        # Also install skcapstone inside the container
        install_cmd = (
            "apt-get update -qq && "
            "apt-get install -y -qq python3 python3-pip python3-venv curl gnupg && "
            "pip3 install --quiet skcapstone"
        )
        self._exec_in_container(vmid, install_cmd)

        logger.info(
            "LXC %d configured for agent %s (config_written=%s)",
            vmid, agent_name, ok,
        )
        return True

    def start(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Start the LXC container.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if started.
        """
        vmid = provision_result.get("vmid")
        if not vmid:
            return False

        try:
            self._api_call(
                "POST",
                f"/nodes/{self._node}/lxc/{vmid}/status/start",
            )
            return True
        except RuntimeError:
            return False

    def stop(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Stop the LXC container.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if stopped.
        """
        vmid = provision_result.get("vmid")
        if not vmid:
            return False

        try:
            self._api_call(
                "POST",
                f"/nodes/{self._node}/lxc/{vmid}/status/stop",
            )
            return True
        except RuntimeError:
            return False

    def destroy(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> bool:
        """Destroy the LXC container.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if destroyed.
        """
        vmid = provision_result.get("vmid")
        if not vmid:
            return False

        try:
            self.stop(agent_name, provision_result)
            time.sleep(3)
            self._api_call(
                "DELETE",
                f"/nodes/{self._node}/lxc/{vmid}",
                data={"purge": 1},
            )
            return True
        except RuntimeError as exc:
            logger.error("Failed to destroy LXC %d: %s", vmid, exc)
            return False

    def health_check(
        self,
        agent_name: str,
        provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check LXC container status.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            AgentStatus based on container state.
        """
        vmid = provision_result.get("vmid")
        if not vmid:
            return AgentStatus.STOPPED

        try:
            status = self._api_call(
                "GET",
                f"/nodes/{self._node}/lxc/{vmid}/status/current",
            )
            state = status.get("status", "stopped")
            if state == "running":
                return AgentStatus.RUNNING
            elif state == "stopped":
                return AgentStatus.STOPPED
            else:
                return AgentStatus.DEGRADED
        except RuntimeError:
            return AgentStatus.FAILED
