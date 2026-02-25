"""
Cloud Provider — deploy agents on Hetzner, AWS, GCP, or any cloud.

This is the abstraction layer that makes blueprints truly portable.
Each cloud gets a thin adapter; the provider interface stays the same.

Currently supports:
- Hetzner Cloud (via hcloud API)
- AWS EC2 (via boto3) — planned
- GCP Compute (via google-cloud) — planned

The pattern: provision a VM/container, install the agent runtime,
configure via cloud-init or SSH, register on the Tailscale mesh.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from ..blueprints.schema import AgentSpec, ProviderType
from ..team_engine import AgentStatus, ProviderBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cloud adapter registry
# ---------------------------------------------------------------------------

_CLOUD_ADAPTERS: Dict[str, type] = {}


def register_cloud_adapter(name: str):
    """Decorator to register a cloud adapter class.

    Args:
        name: Cloud provider name (e.g. 'hetzner', 'aws', 'gcp').
    """
    def wrapper(cls):
        _CLOUD_ADAPTERS[name] = cls
        return cls
    return wrapper


# ---------------------------------------------------------------------------
# Cloud Provider
# ---------------------------------------------------------------------------

class CloudProvider(ProviderBackend):
    """Generic cloud provider that delegates to cloud-specific adapters.

    Args:
        cloud: Which cloud to use ('hetzner', 'aws', 'gcp').
        config: Cloud-specific configuration dict.
    """

    provider_type = ProviderType.HETZNER

    def __init__(
        self,
        cloud: str = "hetzner",
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._cloud = cloud
        self._config = config or {}
        self._adapter = self._get_adapter()

    def _get_adapter(self) -> Any:
        """Instantiate the cloud-specific adapter.

        Returns:
            Cloud adapter instance.

        Raises:
            RuntimeError: If the cloud adapter is not available.
        """
        adapter_cls = _CLOUD_ADAPTERS.get(self._cloud)
        if adapter_cls:
            return adapter_cls(**self._config)

        if self._cloud == "hetzner":
            return HetznerAdapter(**self._config)
        elif self._cloud == "aws":
            raise RuntimeError(
                "AWS adapter coming soon. "
                "Set up AWS credentials and check back."
            )
        elif self._cloud == "gcp":
            raise RuntimeError(
                "GCP adapter coming soon. "
                "Set up GCP credentials and check back."
            )
        else:
            raise RuntimeError(f"Unknown cloud provider: {self._cloud}")

    def provision(
        self, agent_name: str, spec: AgentSpec, team_name: str,
    ) -> Dict[str, Any]:
        """Delegate to cloud adapter."""
        return self._adapter.provision(agent_name, spec, team_name)

    def configure(
        self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any],
    ) -> bool:
        """Delegate to cloud adapter."""
        return self._adapter.configure(agent_name, spec, provision_result)

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Delegate to cloud adapter."""
        return self._adapter.start(agent_name, provision_result)

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Delegate to cloud adapter."""
        return self._adapter.stop(agent_name, provision_result)

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Delegate to cloud adapter."""
        return self._adapter.destroy(agent_name, provision_result)

    def health_check(
        self, agent_name: str, provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Delegate to cloud adapter."""
        return self._adapter.health_check(agent_name, provision_result)


# ---------------------------------------------------------------------------
# Hetzner Adapter
# ---------------------------------------------------------------------------

def _memory_to_hetzner_type(memory_str: str, cores: int) -> str:
    """Map resource spec to closest Hetzner server type.

    Args:
        memory_str: Memory allocation string (e.g. '4g').
        cores: Number of CPU cores.

    Returns:
        Hetzner server type name (e.g. 'cx22', 'cx32').
    """
    mem_str = memory_str.strip().lower()
    if mem_str.endswith("g"):
        mem_gb = float(mem_str[:-1])
    elif mem_str.endswith("m"):
        mem_gb = float(mem_str[:-1]) / 1024
    else:
        mem_gb = float(mem_str) / 1024

    # Hetzner CX line: cx22=4GB/2c, cx32=8GB/4c, cx42=16GB/8c, cx52=32GB/16c
    if mem_gb <= 2 and cores <= 2:
        return "cx22"
    elif mem_gb <= 4 and cores <= 2:
        return "cx22"
    elif mem_gb <= 8 and cores <= 4:
        return "cx32"
    elif mem_gb <= 16 and cores <= 8:
        return "cx42"
    else:
        return "cx52"


@register_cloud_adapter("hetzner")
class HetznerAdapter:
    """Hetzner Cloud adapter using the hcloud API.

    Expects HETZNER_API_TOKEN environment variable or token in config.
    """

    def __init__(self, api_token: Optional[str] = None, **kwargs: Any) -> None:
        self._token = api_token or os.environ.get("HETZNER_API_TOKEN", "")

    def _api_call(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Make an authenticated Hetzner API call.

        Args:
            method: HTTP method.
            endpoint: API endpoint.
            data: Request body.

        Returns:
            Parsed JSON response.

        Raises:
            RuntimeError: On API failure.
        """
        try:
            import requests
        except ImportError:
            raise RuntimeError(
                "Hetzner adapter requires 'requests': pip install requests"
            )

        if not self._token:
            raise RuntimeError(
                "Hetzner not configured. Set HETZNER_API_TOKEN."
            )

        url = f"https://api.hetzner.cloud/v1{endpoint}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

        resp = requests.request(
            method, url, headers=headers, json=data, timeout=30,
        )

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Hetzner API {method} {endpoint}: "
                f"{resp.status_code} {resp.text}"
            )

        return resp.json()

    def provision(
        self, agent_name: str, spec: AgentSpec, team_name: str,
    ) -> Dict[str, Any]:
        """Create a Hetzner Cloud server.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Dict with server details.
        """
        server_type = _memory_to_hetzner_type(
            spec.resources.memory, spec.resources.cores,
        )

        cloud_init = _build_cloud_init(agent_name, spec)

        create_data = {
            "name": agent_name.replace("_", "-")[:63],
            "server_type": server_type,
            "image": "debian-12",
            "location": "fsn1",
            "start_after_create": True,
            "user_data": cloud_init,
            "labels": {
                "team": team_name.replace(" ", "-").lower()[:63],
                "agent": agent_name[:63],
                "role": spec.role.value,
                "managed-by": "skcapstone",
            },
        }

        logger.info(
            "Creating Hetzner server %s (type=%s)",
            agent_name, server_type,
        )

        result = self._api_call("POST", "/servers", data=create_data)
        server = result.get("server", {})

        return {
            "server_id": server.get("id"),
            "host": server.get("public_net", {}).get("ipv4", {}).get("ip", ""),
            "container_id": str(server.get("id", "")),
        }

    def configure(
        self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any],
    ) -> bool:
        """Cloud-init handles configuration at boot."""
        return True

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Power on the server."""
        server_id = provision_result.get("server_id")
        if not server_id:
            return False
        try:
            self._api_call("POST", f"/servers/{server_id}/actions/poweron")
            return True
        except RuntimeError:
            return False

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Power off the server."""
        server_id = provision_result.get("server_id")
        if not server_id:
            return False
        try:
            self._api_call("POST", f"/servers/{server_id}/actions/poweroff")
            return True
        except RuntimeError:
            return False

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Delete the server."""
        server_id = provision_result.get("server_id")
        if not server_id:
            return False
        try:
            self._api_call("DELETE", f"/servers/{server_id}")
            return True
        except RuntimeError as exc:
            logger.error("Failed to destroy Hetzner server %s: %s", server_id, exc)
            return False

    def health_check(
        self, agent_name: str, provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check server status via API."""
        server_id = provision_result.get("server_id")
        if not server_id:
            return AgentStatus.STOPPED
        try:
            result = self._api_call("GET", f"/servers/{server_id}")
            status = result.get("server", {}).get("status", "off")
            if status == "running":
                return AgentStatus.RUNNING
            elif status in ("off", "deleting"):
                return AgentStatus.STOPPED
            else:
                return AgentStatus.DEGRADED
        except RuntimeError:
            return AgentStatus.FAILED


# ---------------------------------------------------------------------------
# Cloud-init template
# ---------------------------------------------------------------------------

def _build_cloud_init(agent_name: str, spec: AgentSpec) -> str:
    """Generate cloud-init user data to bootstrap an agent VM.

    Args:
        agent_name: Agent instance name.
        spec: Agent specification.

    Returns:
        Cloud-init YAML string.
    """
    return f"""#cloud-config
package_update: true
packages:
  - python3
  - python3-pip
  - python3-venv
  - curl
  - gnupg

runcmd:
  - pip3 install skcapstone
  - mkdir -p /opt/agent
  - |
    cat > /opt/agent/config.json << 'AGENT_EOF'
    {{
      "agent_name": "{agent_name}",
      "role": "{spec.role.value}",
      "model": "{spec.model_name or spec.model.value}",
      "skills": {json.dumps(spec.skills)}
    }}
    AGENT_EOF
  - curl -fsSL https://tailscale.com/install.sh | sh
  - echo "Agent {agent_name} provisioned by skcapstone"
"""
