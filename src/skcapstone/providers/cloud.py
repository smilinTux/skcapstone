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
            return AWSAdapter(**self._config)
        elif self._cloud == "gcp":
            return GCPAdapter(**self._config)
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


# ---------------------------------------------------------------------------
# AWS EC2 Adapter
# ---------------------------------------------------------------------------

def _memory_to_ec2_instance_type(memory_str: str, cores: int) -> str:
    """Map resource spec to closest AWS EC2 instance type.

    Args:
        memory_str: Memory allocation string (e.g. '4g').
        cores: Number of CPU cores.

    Returns:
        EC2 instance type name (e.g. 't3.small', 't3.medium').
    """
    mem_str = memory_str.strip().lower()
    if mem_str.endswith("g"):
        mem_gb = float(mem_str[:-1])
    elif mem_str.endswith("m"):
        mem_gb = float(mem_str[:-1]) / 1024
    else:
        mem_gb = float(mem_str) / 1024

    # t3 line: micro=1GB/2c, small=2GB/2c, medium=4GB/2c,
    #          large=8GB/2c, xlarge=16GB/4c, 2xlarge=32GB/8c
    if mem_gb <= 1 and cores <= 2:
        return "t3.micro"
    elif mem_gb <= 2 and cores <= 2:
        return "t3.small"
    elif mem_gb <= 4 and cores <= 2:
        return "t3.medium"
    elif mem_gb <= 8 and cores <= 2:
        return "t3.large"
    elif mem_gb <= 16 and cores <= 4:
        return "t3.xlarge"
    else:
        return "t3.2xlarge"


@register_cloud_adapter("aws")
class AWSAdapter:
    """AWS EC2 adapter using boto3.

    Launches EC2 instances with cloud-init user data for agent bootstrap
    and Tailscale mesh auto-join.

    Expects AWS credentials via environment variables, ~/.aws/credentials,
    or IAM instance profile:
        AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION

    Args:
        region: AWS region (e.g. 'us-east-1'). Falls back to
            AWS_DEFAULT_REGION.
        ami_id: Base AMI ID. Defaults to Debian 12 lookup via SSM.
        security_group_id: Security group for instances. If not provided,
            uses the VPC default.
        subnet_id: Subnet to launch into. If not provided, uses default.
        key_name: EC2 key pair name for SSH access (optional).
    """

    # Default Debian 12 AMIs per region (amd64, hvm, ebs).
    _DEFAULT_AMIS = {
        "us-east-1": "ami-0fec2c2e2017f4e7b",
        "us-west-2": "ami-0b6edd8449255b799",
        "eu-central-1": "ami-042e6fdb154c830c5",
        "eu-west-1": "ami-0694d931cee176e7d",
    }

    def __init__(
        self,
        region: Optional[str] = None,
        ami_id: Optional[str] = None,
        security_group_id: Optional[str] = None,
        subnet_id: Optional[str] = None,
        key_name: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self._region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
        self._ami_id = ami_id
        self._security_group_id = security_group_id
        self._subnet_id = subnet_id
        self._key_name = key_name

    def _ec2_client(self) -> Any:
        """Create a boto3 EC2 client.

        Returns:
            boto3 EC2 client.

        Raises:
            RuntimeError: If boto3 is not installed.
        """
        try:
            import boto3
        except ImportError:
            raise RuntimeError(
                "AWS adapter requires boto3: pip install boto3"
            )
        return boto3.client("ec2", region_name=self._region)

    def _resolve_ami(self) -> str:
        """Return the AMI ID for the target region.

        Uses the explicit ami_id if configured, otherwise falls back to a
        built-in map of Debian 12 AMIs.

        Returns:
            AMI ID string.

        Raises:
            RuntimeError: If no AMI can be resolved for the region.
        """
        if self._ami_id:
            return self._ami_id
        ami = self._DEFAULT_AMIS.get(self._region)
        if ami:
            return ami
        raise RuntimeError(
            f"No default AMI for region {self._region}. "
            "Pass ami_id= explicitly."
        )

    def provision(
        self, agent_name: str, spec: AgentSpec, team_name: str,
    ) -> Dict[str, Any]:
        """Launch an EC2 instance.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Dict with instance_id, host (public IP), container_id.
        """
        ec2 = self._ec2_client()
        instance_type = _memory_to_ec2_instance_type(
            spec.resources.memory, spec.resources.cores,
        )
        ami = self._resolve_ami()
        cloud_init = _build_cloud_init(agent_name, spec)

        run_kwargs: Dict[str, Any] = {
            "ImageId": ami,
            "InstanceType": instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": cloud_init,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": agent_name[:255]},
                        {"Key": "Team", "Value": team_name[:255]},
                        {"Key": "Role", "Value": spec.role.value},
                        {"Key": "ManagedBy", "Value": "skcapstone"},
                    ],
                }
            ],
        }
        if self._key_name:
            run_kwargs["KeyName"] = self._key_name
        if self._security_group_id:
            run_kwargs["SecurityGroupIds"] = [self._security_group_id]
        if self._subnet_id:
            run_kwargs["SubnetId"] = self._subnet_id

        logger.info(
            "Launching EC2 instance %s (type=%s ami=%s region=%s)",
            agent_name, instance_type, ami, self._region,
        )

        result = ec2.run_instances(**run_kwargs)
        instance = result["Instances"][0]
        instance_id = instance["InstanceId"]

        # Wait briefly for public IP assignment.
        public_ip = instance.get("PublicIpAddress", "")
        if not public_ip:
            try:
                waiter = ec2.get_waiter("instance_running")
                waiter.wait(
                    InstanceIds=[instance_id],
                    WaiterConfig={"Delay": 5, "MaxAttempts": 12},
                )
                desc = ec2.describe_instances(InstanceIds=[instance_id])
                reservations = desc.get("Reservations", [])
                if reservations:
                    inst = reservations[0]["Instances"][0]
                    public_ip = inst.get("PublicIpAddress", "")
            except Exception as exc:
                logger.warning("Could not get public IP for %s: %s", instance_id, exc)

        return {
            "instance_id": instance_id,
            "host": public_ip,
            "container_id": instance_id,
            "region": self._region,
        }

    def configure(
        self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any],
    ) -> bool:
        """Cloud-init handles configuration at boot."""
        return True

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Start a stopped EC2 instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the start request succeeded.
        """
        instance_id = provision_result.get("instance_id")
        if not instance_id:
            return False
        try:
            ec2 = self._ec2_client()
            ec2.start_instances(InstanceIds=[instance_id])
            return True
        except Exception as exc:
            logger.error("Failed to start EC2 %s: %s", instance_id, exc)
            return False

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Stop a running EC2 instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the stop request succeeded.
        """
        instance_id = provision_result.get("instance_id")
        if not instance_id:
            return False
        try:
            ec2 = self._ec2_client()
            ec2.stop_instances(InstanceIds=[instance_id])
            return True
        except Exception as exc:
            logger.error("Failed to stop EC2 %s: %s", instance_id, exc)
            return False

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Terminate an EC2 instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the termination request succeeded.
        """
        instance_id = provision_result.get("instance_id")
        if not instance_id:
            return False
        try:
            ec2 = self._ec2_client()
            ec2.terminate_instances(InstanceIds=[instance_id])
            logger.info("Terminated EC2 instance %s", instance_id)
            return True
        except Exception as exc:
            logger.error("Failed to terminate EC2 %s: %s", instance_id, exc)
            return False

    def health_check(
        self, agent_name: str, provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check EC2 instance status.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            AgentStatus based on instance state.
        """
        instance_id = provision_result.get("instance_id")
        if not instance_id:
            return AgentStatus.STOPPED
        try:
            ec2 = self._ec2_client()
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            reservations = desc.get("Reservations", [])
            if not reservations:
                return AgentStatus.STOPPED
            state = reservations[0]["Instances"][0]["State"]["Name"]
            if state == "running":
                return AgentStatus.RUNNING
            elif state in ("stopped", "terminated"):
                return AgentStatus.STOPPED
            elif state in ("pending", "stopping", "shutting-down"):
                return AgentStatus.DEGRADED
            else:
                return AgentStatus.DEGRADED
        except Exception:
            return AgentStatus.FAILED


# ---------------------------------------------------------------------------
# GCP Compute Adapter
# ---------------------------------------------------------------------------

def _memory_to_gcp_machine_type(memory_str: str, cores: int) -> str:
    """Map resource spec to closest GCP machine type.

    Args:
        memory_str: Memory allocation string (e.g. '4g').
        cores: Number of CPU cores.

    Returns:
        GCP machine type name (e.g. 'e2-small', 'e2-medium').
    """
    mem_str = memory_str.strip().lower()
    if mem_str.endswith("g"):
        mem_gb = float(mem_str[:-1])
    elif mem_str.endswith("m"):
        mem_gb = float(mem_str[:-1]) / 1024
    else:
        mem_gb = float(mem_str) / 1024

    # e2 line: micro=1GB/0.25c, small=2GB/0.5c, medium=4GB/1c,
    #          standard-2=8GB/2c, standard-4=16GB/4c, standard-8=32GB/8c
    if mem_gb <= 1 and cores <= 1:
        return "e2-micro"
    elif mem_gb <= 2 and cores <= 1:
        return "e2-small"
    elif mem_gb <= 4 and cores <= 2:
        return "e2-medium"
    elif mem_gb <= 8 and cores <= 2:
        return "e2-standard-2"
    elif mem_gb <= 16 and cores <= 4:
        return "e2-standard-4"
    else:
        return "e2-standard-8"


@register_cloud_adapter("gcp")
class GCPAdapter:
    """GCP Compute Engine adapter using the google-cloud-compute library.

    Launches Compute Engine instances with startup-script metadata for
    agent bootstrap and Tailscale mesh auto-join.

    Expects GCP credentials via:
        GOOGLE_APPLICATION_CREDENTIALS (service account JSON) or
        Application Default Credentials (gcloud auth application-default login).

    Args:
        project: GCP project ID. Falls back to GOOGLE_CLOUD_PROJECT or
            GCLOUD_PROJECT.
        zone: Compute zone (e.g. 'us-central1-a'). Falls back to
            CLOUDSDK_COMPUTE_ZONE.
        network: VPC network name (default: 'default').
        subnet: Subnetwork name (optional).
        service_account_email: Service account for the instance (optional).
    """

    # Debian 12 image family on GCP.
    _IMAGE_PROJECT = "debian-cloud"
    _IMAGE_FAMILY = "debian-12"

    def __init__(
        self,
        project: Optional[str] = None,
        zone: Optional[str] = None,
        network: str = "default",
        subnet: Optional[str] = None,
        service_account_email: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        self._project = (
            project
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("GCLOUD_PROJECT", "")
        )
        self._zone = zone or os.environ.get("CLOUDSDK_COMPUTE_ZONE", "us-central1-a")
        self._network = network
        self._subnet = subnet
        self._service_account_email = service_account_email

    def _compute_client(self) -> Any:
        """Create a GCP Compute instances client.

        Returns:
            google.cloud.compute_v1.InstancesClient.

        Raises:
            RuntimeError: If google-cloud-compute is not installed.
        """
        try:
            from google.cloud import compute_v1
        except ImportError:
            raise RuntimeError(
                "GCP adapter requires google-cloud-compute: "
                "pip install google-cloud-compute"
            )
        return compute_v1.InstancesClient()

    def _get_source_image(self) -> str:
        """Resolve the latest Debian 12 image URI from GCP.

        Returns:
            Fully-qualified image self-link.

        Raises:
            RuntimeError: If the image cannot be resolved.
        """
        try:
            from google.cloud import compute_v1

            images_client = compute_v1.ImagesClient()
            image = images_client.get_from_family(
                project=self._IMAGE_PROJECT, family=self._IMAGE_FAMILY,
            )
            return image.self_link
        except ImportError:
            # Fallback to well-known URI pattern.
            return (
                f"projects/{self._IMAGE_PROJECT}/global/images/family/"
                f"{self._IMAGE_FAMILY}"
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to resolve GCP image: {exc}")

    def provision(
        self, agent_name: str, spec: AgentSpec, team_name: str,
    ) -> Dict[str, Any]:
        """Create a GCP Compute Engine instance.

        Args:
            agent_name: Agent instance name.
            spec: Agent specification.
            team_name: Parent team name.

        Returns:
            Dict with instance_name, zone, host (external IP).
        """
        try:
            from google.cloud import compute_v1
        except ImportError:
            raise RuntimeError(
                "GCP adapter requires google-cloud-compute: "
                "pip install google-cloud-compute"
            )

        if not self._project:
            raise RuntimeError(
                "GCP project not configured. Set GOOGLE_CLOUD_PROJECT "
                "or pass project= to GCPAdapter."
            )

        machine_type = _memory_to_gcp_machine_type(
            spec.resources.memory, spec.resources.cores,
        )
        instance_name = agent_name.replace("_", "-").lower()[:63]
        cloud_init = _build_cloud_init(agent_name, spec)
        source_image = self._get_source_image()

        # Build instance resource.
        instance = compute_v1.Instance()
        instance.name = instance_name
        instance.machine_type = (
            f"zones/{self._zone}/machineTypes/{machine_type}"
        )

        # Boot disk.
        disk = compute_v1.AttachedDisk()
        disk.auto_delete = True
        disk.boot = True
        init_params = compute_v1.AttachedDiskInitializeParams()
        init_params.source_image = source_image
        init_params.disk_size_gb = 20
        disk.initialize_params = init_params
        instance.disks = [disk]

        # Network.
        net_iface = compute_v1.NetworkInterface()
        net_iface.network = f"global/networks/{self._network}"
        if self._subnet:
            net_iface.subnetwork = (
                f"regions/{self._zone.rsplit('-', 1)[0]}/subnetworks/{self._subnet}"
            )
        # External IP for SSH/Tailscale.
        access_config = compute_v1.AccessConfig()
        access_config.name = "External NAT"
        access_config.type_ = "ONE_TO_ONE_NAT"
        net_iface.access_configs = [access_config]
        instance.network_interfaces = [net_iface]

        # Startup script (cloud-init equivalent).
        instance.metadata = compute_v1.Metadata()
        instance.metadata.items = [
            compute_v1.Items(key="startup-script", value=cloud_init),
        ]

        # Labels.
        instance.labels = {
            "team": team_name.replace(" ", "-").lower()[:63],
            "agent": instance_name[:63],
            "role": spec.role.value,
            "managed-by": "skcapstone",
        }

        # Service account.
        if self._service_account_email:
            sa = compute_v1.ServiceAccount()
            sa.email = self._service_account_email
            sa.scopes = ["https://www.googleapis.com/auth/cloud-platform"]
            instance.service_accounts = [sa]

        logger.info(
            "Creating GCP instance %s (type=%s zone=%s project=%s)",
            instance_name, machine_type, self._zone, self._project,
        )

        client = self._compute_client()
        operation = client.insert(
            project=self._project,
            zone=self._zone,
            instance_resource=instance,
        )

        # Wait for the operation to complete.
        try:
            operation.result(timeout=120)
        except Exception as exc:
            logger.warning("GCP insert wait: %s", exc)

        # Fetch instance details for external IP.
        host = ""
        try:
            inst = client.get(
                project=self._project,
                zone=self._zone,
                instance=instance_name,
            )
            for iface in inst.network_interfaces:
                for ac in iface.access_configs:
                    if ac.nat_i_p:
                        host = ac.nat_i_p
                        break
        except Exception as exc:
            logger.warning("Could not fetch GCP instance IP: %s", exc)

        return {
            "instance_name": instance_name,
            "host": host,
            "zone": self._zone,
            "project": self._project,
            "container_id": instance_name,
        }

    def configure(
        self, agent_name: str, spec: AgentSpec, provision_result: Dict[str, Any],
    ) -> bool:
        """Startup script handles configuration at boot."""
        return True

    def start(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Start a stopped GCP instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the start request succeeded.
        """
        instance_name = provision_result.get("instance_name")
        if not instance_name:
            return False
        try:
            client = self._compute_client()
            operation = client.start(
                project=provision_result.get("project", self._project),
                zone=provision_result.get("zone", self._zone),
                instance=instance_name,
            )
            operation.result(timeout=60)
            return True
        except Exception as exc:
            logger.error("Failed to start GCP instance %s: %s", instance_name, exc)
            return False

    def stop(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Stop a running GCP instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the stop request succeeded.
        """
        instance_name = provision_result.get("instance_name")
        if not instance_name:
            return False
        try:
            client = self._compute_client()
            operation = client.stop(
                project=provision_result.get("project", self._project),
                zone=provision_result.get("zone", self._zone),
                instance=instance_name,
            )
            operation.result(timeout=60)
            return True
        except Exception as exc:
            logger.error("Failed to stop GCP instance %s: %s", instance_name, exc)
            return False

    def destroy(self, agent_name: str, provision_result: Dict[str, Any]) -> bool:
        """Delete a GCP instance.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            True if the deletion request succeeded.
        """
        instance_name = provision_result.get("instance_name")
        if not instance_name:
            return False
        try:
            client = self._compute_client()
            operation = client.delete(
                project=provision_result.get("project", self._project),
                zone=provision_result.get("zone", self._zone),
                instance=instance_name,
            )
            operation.result(timeout=120)
            logger.info("Deleted GCP instance %s", instance_name)
            return True
        except Exception as exc:
            logger.error("Failed to delete GCP instance %s: %s", instance_name, exc)
            return False

    def health_check(
        self, agent_name: str, provision_result: Dict[str, Any],
    ) -> AgentStatus:
        """Check GCP instance status.

        Args:
            agent_name: Agent instance name.
            provision_result: Output from provision().

        Returns:
            AgentStatus based on instance state.
        """
        instance_name = provision_result.get("instance_name")
        if not instance_name:
            return AgentStatus.STOPPED
        try:
            client = self._compute_client()
            inst = client.get(
                project=provision_result.get("project", self._project),
                zone=provision_result.get("zone", self._zone),
                instance=instance_name,
            )
            status = inst.status
            if status == "RUNNING":
                return AgentStatus.RUNNING
            elif status in ("TERMINATED", "STOPPED"):
                return AgentStatus.STOPPED
            elif status in ("STAGING", "STOPPING", "SUSPENDING", "SUSPENDED"):
                return AgentStatus.DEGRADED
            else:
                return AgentStatus.DEGRADED
        except Exception:
            return AgentStatus.FAILED
