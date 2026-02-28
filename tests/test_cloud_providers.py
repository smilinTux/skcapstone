"""Tests for AWS EC2 and GCP Compute cloud provider adapters.

All cloud API calls are mocked — no real infrastructure required.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from skcapstone.blueprints.schema import AgentRole, AgentSpec, ModelTier, ResourceSpec
from skcapstone.providers.cloud import (
    AWSAdapter,
    CloudProvider,
    GCPAdapter,
    HetznerAdapter,
    _CLOUD_ADAPTERS,
    _build_cloud_init,
    _memory_to_ec2_instance_type,
    _memory_to_gcp_machine_type,
    _memory_to_hetzner_type,
)
from skcapstone.team_engine import AgentStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    role: str = "worker",
    model: str = "fast",
    memory: str = "4g",
    cores: int = 2,
    skills: list | None = None,
) -> AgentSpec:
    """Build a minimal AgentSpec for testing."""
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        resources=ResourceSpec(memory=memory, cores=cores),
        skills=skills or [],
        env={},
    )


# ---------------------------------------------------------------------------
# Instance type mapping
# ---------------------------------------------------------------------------


class TestMemoryToEC2InstanceType:
    """Tests for _memory_to_ec2_instance_type."""

    def test_micro_for_1g(self):
        assert _memory_to_ec2_instance_type("1g", 1) == "t3.micro"

    def test_small_for_2g(self):
        assert _memory_to_ec2_instance_type("2g", 2) == "t3.small"

    def test_medium_for_4g(self):
        assert _memory_to_ec2_instance_type("4g", 2) == "t3.medium"

    def test_large_for_8g(self):
        assert _memory_to_ec2_instance_type("8g", 2) == "t3.large"

    def test_xlarge_for_16g(self):
        assert _memory_to_ec2_instance_type("16g", 4) == "t3.xlarge"

    def test_2xlarge_for_large_memory(self):
        assert _memory_to_ec2_instance_type("64g", 16) == "t3.2xlarge"

    def test_megabytes_converted(self):
        assert _memory_to_ec2_instance_type("512m", 1) == "t3.micro"


class TestMemoryToGCPMachineType:
    """Tests for _memory_to_gcp_machine_type."""

    def test_micro_for_1g(self):
        assert _memory_to_gcp_machine_type("1g", 1) == "e2-micro"

    def test_small_for_2g(self):
        assert _memory_to_gcp_machine_type("2g", 1) == "e2-small"

    def test_medium_for_4g(self):
        assert _memory_to_gcp_machine_type("4g", 2) == "e2-medium"

    def test_standard_2_for_8g(self):
        assert _memory_to_gcp_machine_type("8g", 2) == "e2-standard-2"

    def test_standard_4_for_16g(self):
        assert _memory_to_gcp_machine_type("16g", 4) == "e2-standard-4"

    def test_standard_8_for_large(self):
        assert _memory_to_gcp_machine_type("64g", 16) == "e2-standard-8"


# ---------------------------------------------------------------------------
# CloudProvider adapter dispatch
# ---------------------------------------------------------------------------


class TestCloudProviderDispatch:
    """Tests for CloudProvider._get_adapter routing."""

    def test_hetzner_returns_hetzner_adapter(self):
        with patch.object(HetznerAdapter, "__init__", return_value=None):
            provider = CloudProvider(cloud="hetzner")
        assert isinstance(provider._adapter, HetznerAdapter)

    def test_aws_returns_aws_adapter(self):
        provider = CloudProvider(cloud="aws")
        assert isinstance(provider._adapter, AWSAdapter)

    def test_gcp_returns_gcp_adapter(self):
        provider = CloudProvider(cloud="gcp")
        assert isinstance(provider._adapter, GCPAdapter)

    def test_unknown_cloud_raises(self):
        with pytest.raises(RuntimeError, match="Unknown cloud"):
            CloudProvider(cloud="digitalocean")

    def test_registered_adapters_include_aws_and_gcp(self):
        assert "aws" in _CLOUD_ADAPTERS
        assert "gcp" in _CLOUD_ADAPTERS


# ---------------------------------------------------------------------------
# AWS EC2 Adapter
# ---------------------------------------------------------------------------


class TestAWSAdapterProvision:
    """Tests for AWSAdapter.provision()."""

    @pytest.fixture()
    def adapter(self):
        return AWSAdapter(region="us-east-1", ami_id="ami-test123")

    @pytest.fixture()
    def mock_ec2(self):
        mock = MagicMock()
        mock.run_instances.return_value = {
            "Instances": [{
                "InstanceId": "i-abc123",
                "PublicIpAddress": "1.2.3.4",
            }]
        }
        return mock

    def test_provision_returns_instance_id(self, adapter, mock_ec2):
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.provision("agent-1", _make_spec(), "team-a")
        assert result["instance_id"] == "i-abc123"
        assert result["host"] == "1.2.3.4"

    def test_provision_tags_instance(self, adapter, mock_ec2):
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            adapter.provision("agent-1", _make_spec(role="coder"), "team-a")
        call_kwargs = mock_ec2.run_instances.call_args[1]
        tags = call_kwargs["TagSpecifications"][0]["Tags"]
        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["Name"] == "agent-1"
        assert tag_dict["Role"] == "coder"
        assert tag_dict["ManagedBy"] == "skcapstone"

    def test_provision_uses_cloud_init(self, adapter, mock_ec2):
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            adapter.provision("agent-1", _make_spec(), "team-a")
        call_kwargs = mock_ec2.run_instances.call_args[1]
        assert "skcapstone" in call_kwargs["UserData"]

    def test_provision_uses_correct_instance_type(self, adapter, mock_ec2):
        spec = _make_spec(memory="8g", cores=2)
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            adapter.provision("agent-1", spec, "team-a")
        call_kwargs = mock_ec2.run_instances.call_args[1]
        assert call_kwargs["InstanceType"] == "t3.large"

    def test_provision_waits_for_ip_if_missing(self, adapter):
        mock_ec2 = MagicMock()
        mock_ec2.run_instances.return_value = {
            "Instances": [{"InstanceId": "i-noip"}]
        }
        mock_waiter = MagicMock()
        mock_ec2.get_waiter.return_value = mock_waiter
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{
                "Instances": [{"PublicIpAddress": "5.6.7.8"}]
            }]
        }
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.provision("agent-wait", _make_spec(), "team-a")
        assert result["host"] == "5.6.7.8"
        mock_waiter.wait.assert_called_once()


class TestAWSAdapterLifecycle:
    """Tests for AWSAdapter start/stop/destroy/health_check."""

    @pytest.fixture()
    def adapter(self):
        return AWSAdapter(region="us-east-1")

    @pytest.fixture()
    def provision_result(self):
        return {"instance_id": "i-test123", "host": "1.2.3.4", "region": "us-east-1"}

    def test_configure_returns_true(self, adapter):
        assert adapter.configure("a", _make_spec(), {}) is True

    def test_start_calls_start_instances(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.start("agent-1", provision_result)
        assert result is True
        mock_ec2.start_instances.assert_called_once_with(InstanceIds=["i-test123"])

    def test_start_returns_false_without_instance_id(self, adapter):
        assert adapter.start("a", {}) is False

    def test_stop_calls_stop_instances(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.stop("agent-1", provision_result)
        assert result is True
        mock_ec2.stop_instances.assert_called_once_with(InstanceIds=["i-test123"])

    def test_stop_returns_false_without_instance_id(self, adapter):
        assert adapter.stop("a", {}) is False

    def test_destroy_calls_terminate_instances(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.destroy("agent-1", provision_result)
        assert result is True
        mock_ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-test123"])

    def test_destroy_returns_false_without_instance_id(self, adapter):
        assert adapter.destroy("a", {}) is False

    def test_destroy_returns_false_on_error(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.terminate_instances.side_effect = Exception("access denied")
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            result = adapter.destroy("agent-1", provision_result)
        assert result is False

    def test_health_running(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "running"}}]}]
        }
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.RUNNING

    def test_health_stopped(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "stopped"}}]}]
        }
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.STOPPED

    def test_health_terminated(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "terminated"}}]}]
        }
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.STOPPED

    def test_health_pending_is_degraded(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {
            "Reservations": [{"Instances": [{"State": {"Name": "pending"}}]}]
        }
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.DEGRADED

    def test_health_no_reservations_returns_stopped(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.return_value = {"Reservations": []}
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.STOPPED

    def test_health_api_error_returns_failed(self, adapter, provision_result):
        mock_ec2 = MagicMock()
        mock_ec2.describe_instances.side_effect = Exception("timeout")
        with patch.object(adapter, "_ec2_client", return_value=mock_ec2):
            status = adapter.health_check("agent-1", provision_result)
        assert status == AgentStatus.FAILED

    def test_health_no_instance_id_returns_stopped(self, adapter):
        assert adapter.health_check("a", {}) == AgentStatus.STOPPED


class TestAWSAdapterAMI:
    """Tests for AWS AMI resolution."""

    def test_explicit_ami_used(self):
        adapter = AWSAdapter(ami_id="ami-custom")
        assert adapter._resolve_ami() == "ami-custom"

    def test_default_ami_for_known_region(self):
        adapter = AWSAdapter(region="us-east-1")
        ami = adapter._resolve_ami()
        assert ami.startswith("ami-")

    def test_unknown_region_raises(self):
        adapter = AWSAdapter(region="ap-southeast-99")
        with pytest.raises(RuntimeError, match="No default AMI"):
            adapter._resolve_ami()


# ---------------------------------------------------------------------------
# GCP Compute Adapter
# ---------------------------------------------------------------------------


class TestGCPAdapterProvision:
    """Tests for GCPAdapter.provision()."""

    @pytest.fixture()
    def adapter(self):
        return GCPAdapter(project="test-project", zone="us-central1-a")

    @pytest.fixture()
    def _mock_compute_v1(self):
        """Mock the google.cloud.compute_v1 module so provision() can import it."""
        mock_mod = MagicMock()
        with patch.dict("sys.modules", {"google": MagicMock(), "google.cloud": MagicMock(), "google.cloud.compute_v1": mock_mod}):
            yield mock_mod

    def test_provision_calls_insert(self, adapter, _mock_compute_v1):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_client.insert.return_value = mock_op

        # Mock get for fetching IP
        mock_inst = MagicMock()
        mock_iface = MagicMock()
        mock_ac = MagicMock()
        mock_ac.nat_i_p = "10.0.0.1"
        mock_iface.access_configs = [mock_ac]
        mock_inst.network_interfaces = [mock_iface]
        mock_client.get.return_value = mock_inst

        with patch.object(adapter, "_compute_client", return_value=mock_client):
            with patch.object(adapter, "_get_source_image", return_value="projects/debian-cloud/global/images/debian-12"):
                result = adapter.provision("agent-gcp", _make_spec(), "team-g")

        assert result["instance_name"] == "agent-gcp"
        assert result["host"] == "10.0.0.1"
        assert result["project"] == "test-project"
        mock_client.insert.assert_called_once()

    def test_provision_no_project_raises(self, _mock_compute_v1):
        adapter = GCPAdapter(project="")
        adapter._project = ""
        with pytest.raises(RuntimeError, match="project not configured"):
            adapter.provision("agent", _make_spec(), "team")

    def test_provision_normalizes_instance_name(self, adapter, _mock_compute_v1):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_client.insert.return_value = mock_op
        mock_client.get.side_effect = Exception("skip")

        with patch.object(adapter, "_compute_client", return_value=mock_client):
            with patch.object(adapter, "_get_source_image", return_value="image"):
                result = adapter.provision("Agent_Name_1", _make_spec(), "team")

        assert result["instance_name"] == "agent-name-1"


class TestGCPAdapterLifecycle:
    """Tests for GCPAdapter start/stop/destroy/health_check."""

    @pytest.fixture()
    def adapter(self):
        return GCPAdapter(project="test-project", zone="us-central1-a")

    @pytest.fixture()
    def provision_result(self):
        return {
            "instance_name": "agent-gcp",
            "host": "10.0.0.1",
            "zone": "us-central1-a",
            "project": "test-project",
        }

    def test_configure_returns_true(self, adapter):
        assert adapter.configure("a", _make_spec(), {}) is True

    def test_start_calls_client_start(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_client.start.return_value = mock_op
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            result = adapter.start("agent-gcp", provision_result)
        assert result is True
        mock_client.start.assert_called_once_with(
            project="test-project",
            zone="us-central1-a",
            instance="agent-gcp",
        )

    def test_start_returns_false_without_instance_name(self, adapter):
        assert adapter.start("a", {}) is False

    def test_stop_calls_client_stop(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_client.stop.return_value = mock_op
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            result = adapter.stop("agent-gcp", provision_result)
        assert result is True
        mock_client.stop.assert_called_once()

    def test_stop_returns_false_without_instance_name(self, adapter):
        assert adapter.stop("a", {}) is False

    def test_destroy_calls_client_delete(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_op = MagicMock()
        mock_client.delete.return_value = mock_op
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            result = adapter.destroy("agent-gcp", provision_result)
        assert result is True
        mock_client.delete.assert_called_once()

    def test_destroy_returns_false_without_instance_name(self, adapter):
        assert adapter.destroy("a", {}) is False

    def test_destroy_returns_false_on_error(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_client.delete.side_effect = Exception("permission denied")
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            result = adapter.destroy("agent-gcp", provision_result)
        assert result is False

    def test_health_running(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_inst = MagicMock()
        mock_inst.status = "RUNNING"
        mock_client.get.return_value = mock_inst
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            status = adapter.health_check("agent-gcp", provision_result)
        assert status == AgentStatus.RUNNING

    def test_health_terminated(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_inst = MagicMock()
        mock_inst.status = "TERMINATED"
        mock_client.get.return_value = mock_inst
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            status = adapter.health_check("agent-gcp", provision_result)
        assert status == AgentStatus.STOPPED

    def test_health_staging_is_degraded(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_inst = MagicMock()
        mock_inst.status = "STAGING"
        mock_client.get.return_value = mock_inst
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            status = adapter.health_check("agent-gcp", provision_result)
        assert status == AgentStatus.DEGRADED

    def test_health_api_error_returns_failed(self, adapter, provision_result):
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("timeout")
        with patch.object(adapter, "_compute_client", return_value=mock_client):
            status = adapter.health_check("agent-gcp", provision_result)
        assert status == AgentStatus.FAILED

    def test_health_no_instance_name_returns_stopped(self, adapter):
        assert adapter.health_check("a", {}) == AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# Cloud-init template
# ---------------------------------------------------------------------------


class TestBuildCloudInit:
    """Tests for _build_cloud_init shared template."""

    def test_contains_agent_name(self):
        spec = _make_spec()
        ci = _build_cloud_init("my-agent", spec)
        assert "my-agent" in ci

    def test_contains_skcapstone_install(self):
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec)
        assert "pip3 install skcapstone" in ci

    def test_contains_tailscale(self):
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec)
        assert "tailscale" in ci

    def test_contains_role(self):
        spec = _make_spec(role="coder")
        ci = _build_cloud_init("agent", spec)
        assert "coder" in ci

    def test_is_cloud_config(self):
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec)
        assert ci.startswith("#cloud-config")
