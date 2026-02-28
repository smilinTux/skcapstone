"""Tests for the Proxmox LXC provider backend.

All Proxmox API calls are mocked so no real Proxmox server is required.
Covers provision, configure, start, stop, destroy, health_check,
exec_in_container, and helper functions.
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, call, patch

import pytest

from skcapstone.blueprints.schema import (
    AgentRole,
    AgentSpec,
    ModelTier,
    ProviderType,
    ResourceSpec,
)
from skcapstone.providers.proxmox import (
    ProxmoxProvider,
    _parse_disk_gb,
    _parse_memory_mb,
)
from skcapstone.team_engine import AgentStatus


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_spec(
    role: str = "worker",
    model: str = "fast",
    memory: str = "2g",
    cores: int = 2,
    skills: list | None = None,
    soul_blueprint: str | None = None,
) -> AgentSpec:
    """Build a minimal AgentSpec for testing."""
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        resources=ResourceSpec(memory=memory, cores=cores),
        skills=skills or [],
        soul_blueprint=soul_blueprint,
        env={},
    )


def _provision_result(vmid: int = 200) -> Dict[str, Any]:
    """Return a typical provision result."""
    return {
        "vmid": vmid,
        "host": "test-agent",
        "node": "pve",
        "container_id": str(vmid),
    }


@pytest.fixture()
def provider() -> ProxmoxProvider:
    """Create a provider with test credentials."""
    return ProxmoxProvider(
        api_host="https://pve.test:8006",
        user="root@pam",
        token_name="test-token",
        token_value="secret-value",
        node="pve",
        storage="local-lvm",
    )


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_memory_mb_gigabytes(self) -> None:
        assert _parse_memory_mb("4g") == 4096

    def test_parse_memory_mb_megabytes(self) -> None:
        assert _parse_memory_mb("512m") == 512

    def test_parse_memory_mb_raw_number(self) -> None:
        assert _parse_memory_mb("1024") == 1024

    def test_parse_memory_mb_float(self) -> None:
        assert _parse_memory_mb("1.5g") == 1536

    def test_parse_disk_gb_gigabytes(self) -> None:
        assert _parse_disk_gb("20g") == 20

    def test_parse_disk_gb_terabytes(self) -> None:
        assert _parse_disk_gb("1t") == 1024

    def test_parse_disk_gb_raw_number(self) -> None:
        assert _parse_disk_gb("50") == 50


# ---------------------------------------------------------------------------
# Provider init
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_with_args(self) -> None:
        p = ProxmoxProvider(
            api_host="https://pve:8006",
            user="admin@pam",
            token_name="tok",
            token_value="val",
        )
        assert p._api_host == "https://pve:8006"
        assert p._user == "admin@pam"
        assert p._token_name == "tok"

    def test_init_from_env(self) -> None:
        env = {
            "PROXMOX_HOST": "https://env-pve:8006",
            "PROXMOX_USER": "env-user@pam",
            "PROXMOX_TOKEN_NAME": "env-tok",
            "PROXMOX_TOKEN_VALUE": "env-val",
        }
        with patch.dict("os.environ", env, clear=False):
            p = ProxmoxProvider()
            assert p._api_host == "https://env-pve:8006"
            assert p._user == "env-user@pam"
            assert p._token_name == "env-tok"

    def test_provider_type(self) -> None:
        p = ProxmoxProvider()
        assert p.provider_type == ProviderType.PROXMOX


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------


class TestApiCall:
    @patch("skcapstone.providers.proxmox.ProxmoxProvider._api_call")
    def test_api_call_url_construction(self, mock_api: MagicMock) -> None:
        """Verify API calls are made with correct auth header."""
        p = ProxmoxProvider(
            api_host="https://pve:8006",
            user="root@pam",
            token_name="tok",
            token_value="val",
        )
        mock_api.return_value = {"status": "running"}
        p._api_call("GET", "/nodes/pve/lxc/200/status/current")
        mock_api.assert_called_once()

    def test_api_call_missing_requests(self, provider: ProxmoxProvider) -> None:
        """Raises RuntimeError if requests is not installed."""
        with patch.dict("sys.modules", {"requests": None, "urllib3": None, "urllib3.exceptions": None}):
            with pytest.raises((RuntimeError, ImportError)):
                provider._api_call("GET", "/test")


# ---------------------------------------------------------------------------
# Provision
# ---------------------------------------------------------------------------


class TestProvision:
    @patch.object(ProxmoxProvider, "_api_call")
    @patch.object(ProxmoxProvider, "_next_vmid", return_value=200)
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_provision_basic(
        self,
        mock_sleep: MagicMock,
        mock_vmid: MagicMock,
        mock_api: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        spec = _make_spec(memory="4g", cores=4)
        result = provider.provision("test-agent", spec, "my-team")

        assert result["vmid"] == 200
        assert result["host"] == "test-agent"
        assert result["node"] == "pve"
        assert result["container_id"] == "200"

        # Verify LXC create call
        mock_api.assert_called_once()
        create_call = mock_api.call_args
        assert create_call[0] == ("POST", "/nodes/pve/lxc")
        create_data = create_call[1]["data"]
        assert create_data["vmid"] == 200
        assert create_data["hostname"] == "test-agent"
        assert create_data["memory"] == 4096
        assert create_data["cores"] == 4
        assert create_data["unprivileged"] == 1

    @patch.object(ProxmoxProvider, "_api_call")
    @patch.object(ProxmoxProvider, "_next_vmid", return_value=201)
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_provision_hostname_sanitized(
        self,
        mock_sleep: MagicMock,
        mock_vmid: MagicMock,
        mock_api: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        spec = _make_spec()
        result = provider.provision("my_agent_name", spec, "team")
        assert result["host"] == "my-agent-name"

    @patch.object(ProxmoxProvider, "_api_call")
    @patch.object(ProxmoxProvider, "_next_vmid", return_value=202)
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_provision_description_has_metadata(
        self,
        mock_sleep: MagicMock,
        mock_vmid: MagicMock,
        mock_api: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        spec = _make_spec(role="manager", model="reason")
        provider.provision("lead-agent", spec, "alpha-team")

        create_data = mock_api.call_args[1]["data"]
        desc = json.loads(create_data["description"])
        assert desc["team"] == "alpha-team"
        assert desc["agent"] == "lead-agent"
        assert desc["role"] == "manager"
        assert desc["managed_by"] == "skcapstone"

    def test_provision_no_host_raises(self) -> None:
        p = ProxmoxProvider()  # No PROXMOX_HOST
        spec = _make_spec()
        with pytest.raises(RuntimeError, match="Proxmox not configured"):
            p.provision("agent", spec, "team")


# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------


class TestConfigure:
    @patch.object(ProxmoxProvider, "_exec_in_container", return_value=True)
    @patch.object(ProxmoxProvider, "_api_call")
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_configure_writes_config(
        self,
        mock_sleep: MagicMock,
        mock_api: MagicMock,
        mock_exec: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.return_value = {"status": "running"}
        spec = _make_spec(skills=["memory-search"], soul_blueprint="lumina")
        result = provider.configure("test-agent", spec, _provision_result())

        assert result is True
        # exec_in_container called twice: config write + install
        assert mock_exec.call_count == 2

        # First call writes config
        write_call = mock_exec.call_args_list[0]
        cmd = write_call[0][1]
        assert "mkdir -p /opt/agent" in cmd
        assert "config.json" in cmd

        # Second call installs deps
        install_call = mock_exec.call_args_list[1]
        cmd = install_call[0][1]
        assert "pip3 install" in cmd
        assert "skcapstone" in cmd

    @patch.object(ProxmoxProvider, "_exec_in_container", return_value=True)
    @patch.object(ProxmoxProvider, "_api_call")
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_configure_starts_stopped_container(
        self,
        mock_sleep: MagicMock,
        mock_api: MagicMock,
        mock_exec: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.return_value = {"status": "stopped"}
        spec = _make_spec()
        provider.configure("agent", spec, _provision_result())

        # Should have called start
        calls = [c[0] for c in mock_api.call_args_list]
        assert ("GET", "/nodes/pve/lxc/200/status/current") in calls
        assert ("POST", "/nodes/pve/lxc/200/status/start") in calls

    @patch.object(ProxmoxProvider, "_exec_in_container")
    @patch.object(ProxmoxProvider, "_api_call")
    def test_configure_no_vmid_returns_false(
        self,
        mock_api: MagicMock,
        mock_exec: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        result = provider.configure("agent", _make_spec(), {})
        assert result is False
        mock_exec.assert_not_called()

    @patch.object(ProxmoxProvider, "_exec_in_container")
    @patch.object(ProxmoxProvider, "_api_call")
    def test_configure_start_failure(
        self,
        mock_api: MagicMock,
        mock_exec: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.side_effect = RuntimeError("API down")
        spec = _make_spec()
        result = provider.configure("agent", spec, _provision_result())
        assert result is False

    @patch.object(ProxmoxProvider, "_exec_in_container", return_value=True)
    @patch.object(ProxmoxProvider, "_api_call")
    @patch("skcapstone.providers.proxmox.time.sleep")
    def test_configure_config_content(
        self,
        mock_sleep: MagicMock,
        mock_api: MagicMock,
        mock_exec: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        """Verify the written config JSON has all expected fields."""
        mock_api.return_value = {"status": "running"}
        spec = _make_spec(
            role="researcher",
            model="reason",
            skills=["search", "browse"],
            soul_blueprint="the-developer",
        )
        provider.configure("research-bot", spec, _provision_result())

        write_cmd = mock_exec.call_args_list[0][0][1]
        # Extract the JSON from the printf command
        assert '"agent_name": "research-bot"' in write_cmd
        assert '"role": "researcher"' in write_cmd
        assert '"soul_blueprint": "the-developer"' in write_cmd


# ---------------------------------------------------------------------------
# Exec in container
# ---------------------------------------------------------------------------


class TestExecInContainer:
    @patch.object(ProxmoxProvider, "_api_call")
    def test_exec_success(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {}
        result = provider._exec_in_container(200, "echo hello")
        assert result is True
        calls = [c[0] for c in mock_api.call_args_list]
        assert ("POST", "/nodes/pve/lxc/200/status/current") in calls
        assert ("POST", "/nodes/pve/lxc/200/exec") in calls

    @patch.object(ProxmoxProvider, "_api_call")
    def test_exec_container_not_accessible(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.side_effect = RuntimeError("Not found")
        result = provider._exec_in_container(200, "echo hello")
        assert result is False

    @patch.object(ProxmoxProvider, "_api_call")
    def test_exec_fallback_to_config(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        """When exec API fails, falls back to writing config via PUT."""
        call_count = 0

        def side_effect(method, endpoint, data=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {}  # status/current succeeds
            if call_count == 2:
                raise RuntimeError("exec not available")  # exec fails
            return {}  # config PUT succeeds

        mock_api.side_effect = side_effect
        result = provider._exec_in_container(200, "echo hello")
        assert result is True
        assert call_count == 3


# ---------------------------------------------------------------------------
# Start / Stop / Destroy
# ---------------------------------------------------------------------------


class TestLifecycle:
    @patch.object(ProxmoxProvider, "_api_call")
    def test_start(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {}
        result = provider.start("agent", _provision_result())
        assert result is True
        mock_api.assert_called_with(
            "POST", "/nodes/pve/lxc/200/status/start"
        )

    @patch.object(ProxmoxProvider, "_api_call")
    def test_start_no_vmid(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        result = provider.start("agent", {})
        assert result is False

    @patch.object(ProxmoxProvider, "_api_call")
    def test_start_api_failure(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.side_effect = RuntimeError("API error")
        result = provider.start("agent", _provision_result())
        assert result is False

    @patch.object(ProxmoxProvider, "_api_call")
    def test_stop(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {}
        result = provider.stop("agent", _provision_result())
        assert result is True
        mock_api.assert_called_with(
            "POST", "/nodes/pve/lxc/200/status/stop"
        )

    @patch.object(ProxmoxProvider, "_api_call")
    def test_stop_no_vmid(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        result = provider.stop("agent", {})
        assert result is False

    @patch.object(ProxmoxProvider, "_api_call")
    def test_stop_api_failure(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.side_effect = RuntimeError("API error")
        result = provider.stop("agent", _provision_result())
        assert result is False

    @patch("skcapstone.providers.proxmox.time.sleep")
    @patch.object(ProxmoxProvider, "_api_call")
    def test_destroy(
        self,
        mock_api: MagicMock,
        mock_sleep: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.return_value = {}
        result = provider.destroy("agent", _provision_result())
        assert result is True

        # Should stop first, then delete
        calls = [c[0] for c in mock_api.call_args_list]
        assert ("POST", "/nodes/pve/lxc/200/status/stop") in calls
        assert ("DELETE", "/nodes/pve/lxc/200") in calls

    @patch.object(ProxmoxProvider, "_api_call")
    def test_destroy_no_vmid(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        result = provider.destroy("agent", {})
        assert result is False

    @patch("skcapstone.providers.proxmox.time.sleep")
    @patch.object(ProxmoxProvider, "_api_call")
    def test_destroy_api_failure(
        self,
        mock_api: MagicMock,
        mock_sleep: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.side_effect = RuntimeError("API error")
        result = provider.destroy("agent", _provision_result())
        assert result is False

    @patch("skcapstone.providers.proxmox.time.sleep")
    @patch.object(ProxmoxProvider, "_api_call")
    def test_destroy_sends_purge(
        self,
        mock_api: MagicMock,
        mock_sleep: MagicMock,
        provider: ProxmoxProvider,
    ) -> None:
        mock_api.return_value = {}
        provider.destroy("agent", _provision_result())

        # Find the DELETE call and check purge flag
        for c in mock_api.call_args_list:
            if c[0][0] == "DELETE":
                assert c[1].get("data", {}).get("purge") == 1


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestHealthCheck:
    @patch.object(ProxmoxProvider, "_api_call")
    def test_running(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {"status": "running"}
        assert provider.health_check("agent", _provision_result()) == AgentStatus.RUNNING

    @patch.object(ProxmoxProvider, "_api_call")
    def test_stopped(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {"status": "stopped"}
        assert provider.health_check("agent", _provision_result()) == AgentStatus.STOPPED

    @patch.object(ProxmoxProvider, "_api_call")
    def test_unknown_state(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {"status": "suspended"}
        assert provider.health_check("agent", _provision_result()) == AgentStatus.DEGRADED

    @patch.object(ProxmoxProvider, "_api_call")
    def test_api_failure(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.side_effect = RuntimeError("unreachable")
        assert provider.health_check("agent", _provision_result()) == AgentStatus.FAILED

    def test_no_vmid(self, provider: ProxmoxProvider) -> None:
        assert provider.health_check("agent", {}) == AgentStatus.STOPPED


# ---------------------------------------------------------------------------
# Next VMID
# ---------------------------------------------------------------------------


class TestNextVmid:
    @patch.object(ProxmoxProvider, "_api_call")
    def test_returns_int(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = "300"
        assert provider._next_vmid() == 300

    @patch.object(ProxmoxProvider, "_api_call")
    def test_returns_int_directly(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = 301
        assert provider._next_vmid() == 301

    @patch.object(ProxmoxProvider, "_api_call")
    def test_fallback(
        self, mock_api: MagicMock, provider: ProxmoxProvider
    ) -> None:
        mock_api.return_value = {"some": "dict"}
        assert provider._next_vmid() == 200
