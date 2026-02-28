"""Tests for CloudProvider — Hetzner adapter and provider_type property.

Focuses on HetznerAdapter (full lifecycle) and CloudProvider.provider_type
which are not covered in test_cloud_providers.py.  All Hetzner API calls
are mocked via requests — no real cloud account required.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.blueprints.schema import AgentRole, AgentSpec, ModelTier, ProviderType, ResourceSpec
from skcapstone.providers.cloud import (
    CloudProvider,
    HetznerAdapter,
    _build_cloud_init,
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
    return AgentSpec(
        role=AgentRole(role),
        model=ModelTier(model),
        resources=ResourceSpec(memory=memory, cores=cores),
        skills=skills or [],
        env={},
    )


# ---------------------------------------------------------------------------
# _memory_to_hetzner_type
# ---------------------------------------------------------------------------


class TestMemoryToHetznerType:
    def test_small_instance_cx22(self):
        assert _memory_to_hetzner_type("2g", 2) == "cx22"

    def test_cx22_for_4g_2c(self):
        assert _memory_to_hetzner_type("4g", 2) == "cx22"

    def test_cx32_for_8g_4c(self):
        assert _memory_to_hetzner_type("8g", 4) == "cx32"

    def test_cx42_for_16g(self):
        assert _memory_to_hetzner_type("16g", 8) == "cx42"

    def test_cx52_for_large(self):
        assert _memory_to_hetzner_type("64g", 32) == "cx52"

    def test_megabytes_converted(self):
        assert _memory_to_hetzner_type("512m", 1) == "cx22"


# ---------------------------------------------------------------------------
# HetznerAdapter._api_call
# ---------------------------------------------------------------------------


class TestHetznerApiCall:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="test-token-abc")

    def _mock_response(self, status_code: int, data: Any) -> MagicMock:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.text = str(data)
        return resp

    def test_successful_get_returns_json(self, adapter):
        mock_resp = self._mock_response(200, {"servers": []})
        with patch("requests.request", return_value=mock_resp):
            result = adapter._api_call("GET", "/servers")
        assert result == {"servers": []}

    def test_raises_on_4xx(self, adapter):
        mock_resp = self._mock_response(404, {"error": "not found"})
        with patch("requests.request", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Hetzner API"):
                adapter._api_call("GET", "/servers/999")

    def test_raises_on_5xx(self, adapter):
        mock_resp = self._mock_response(500, "internal server error")
        with patch("requests.request", return_value=mock_resp):
            with pytest.raises(RuntimeError):
                adapter._api_call("DELETE", "/servers/1")

    def test_raises_without_token(self):
        adapter = HetznerAdapter(api_token="")
        with patch("requests.request"):
            with pytest.raises(RuntimeError, match="HETZNER_API_TOKEN"):
                adapter._api_call("GET", "/servers")

    def test_raises_without_requests_sdk(self, adapter):
        with patch.dict("sys.modules", {"requests": None}):
            with pytest.raises((RuntimeError, ImportError)):
                adapter._api_call("GET", "/servers")

    def test_authorization_header_bearer(self, adapter):
        mock_resp = self._mock_response(200, {})
        with patch("requests.request", return_value=mock_resp) as mock_req:
            adapter._api_call("GET", "/servers")
        headers = mock_req.call_args[1]["headers"]
        assert "Bearer test-token-abc" in headers["Authorization"]


# ---------------------------------------------------------------------------
# HetznerAdapter.provision
# ---------------------------------------------------------------------------


class TestHetznerAdapterProvision:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="tok")

    def test_provision_returns_server_id_and_host(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {
            "server": {
                "id": 12345,
                "public_net": {"ipv4": {"ip": "1.2.3.4"}},
            }
        }
        with patch("requests.request", return_value=mock_resp):
            result = adapter.provision("agent-hz", _make_spec(), "my-team")
        assert result["server_id"] == 12345
        assert result["host"] == "1.2.3.4"
        assert result["container_id"] == "12345"

    def test_provision_applies_correct_server_type(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"server": {"id": 100, "public_net": {"ipv4": {"ip": ""}}}}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            adapter.provision("agent-hz", _make_spec(memory="8g", cores=4), "team")
        post_data = mock_req.call_args[1]["json"]
        assert post_data["server_type"] == "cx32"

    def test_provision_includes_cloud_init(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"server": {"id": 101, "public_net": {"ipv4": {"ip": ""}}}}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            adapter.provision("agent-hz", _make_spec(), "team")
        post_data = mock_req.call_args[1]["json"]
        assert "user_data" in post_data
        assert "#cloud-config" in post_data["user_data"]

    def test_provision_sets_labels(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"server": {"id": 102, "public_net": {"ipv4": {"ip": ""}}}}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            adapter.provision("my-agent", _make_spec(role="coder"), "ops-team")
        post_data = mock_req.call_args[1]["json"]
        labels = post_data.get("labels", {})
        assert labels.get("managed-by") == "skcapstone"
        assert labels.get("role") == "coder"

    def test_provision_normalizes_server_name(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"server": {"id": 103, "public_net": {"ipv4": {"ip": ""}}}}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            adapter.provision("agent_underscore", _make_spec(), "team")
        post_data = mock_req.call_args[1]["json"]
        assert "_" not in post_data["name"]

    def test_provision_handles_missing_ip(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"server": {"id": 104, "public_net": {}}}
        with patch("requests.request", return_value=mock_resp):
            result = adapter.provision("agent-hz", _make_spec(), "team")
        assert result["host"] == ""
        assert result["server_id"] == 104


# ---------------------------------------------------------------------------
# HetznerAdapter.configure
# ---------------------------------------------------------------------------


class TestHetznerAdapterConfigure:
    def test_configure_returns_true(self):
        adapter = HetznerAdapter(api_token="tok")
        assert adapter.configure("a", _make_spec(), {}) is True


# ---------------------------------------------------------------------------
# HetznerAdapter.start
# ---------------------------------------------------------------------------


class TestHetznerAdapterStart:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="tok")

    def test_start_calls_poweron(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            result = adapter.start("a", {"server_id": 100})
        assert result is True
        url = mock_req.call_args[0][1]
        assert "poweron" in url

    def test_start_returns_false_without_server_id(self, adapter):
        assert adapter.start("a", {}) is False

    def test_start_returns_false_on_api_error(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.text = "not found"
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp):
            result = adapter.start("a", {"server_id": 100})
        assert result is False


# ---------------------------------------------------------------------------
# HetznerAdapter.stop
# ---------------------------------------------------------------------------


class TestHetznerAdapterStop:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="tok")

    def test_stop_calls_poweroff(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            result = adapter.stop("a", {"server_id": 100})
        assert result is True
        url = mock_req.call_args[0][1]
        assert "poweroff" in url

    def test_stop_returns_false_without_server_id(self, adapter):
        assert adapter.stop("a", {}) is False

    def test_stop_returns_false_on_api_error(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "error"
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp):
            result = adapter.stop("a", {"server_id": 100})
        assert result is False


# ---------------------------------------------------------------------------
# HetznerAdapter.destroy
# ---------------------------------------------------------------------------


class TestHetznerAdapterDestroy:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="tok")

    def test_destroy_calls_delete(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp) as mock_req:
            result = adapter.destroy("a", {"server_id": 100})
        assert result is True
        method, url = mock_req.call_args[0]
        assert method == "DELETE"
        assert "100" in url

    def test_destroy_returns_false_without_server_id(self, adapter):
        assert adapter.destroy("a", {}) is False

    def test_destroy_returns_false_on_error(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.text = "forbidden"
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp):
            result = adapter.destroy("a", {"server_id": 100})
        assert result is False


# ---------------------------------------------------------------------------
# HetznerAdapter.health_check
# ---------------------------------------------------------------------------


class TestHetznerAdapterHealthCheck:
    @pytest.fixture()
    def adapter(self):
        return HetznerAdapter(api_token="tok")

    def test_health_running(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"server": {"status": "running"}}
        with patch("requests.request", return_value=mock_resp):
            status = adapter.health_check("a", {"server_id": 100})
        assert status == AgentStatus.RUNNING

    def test_health_off(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"server": {"status": "off"}}
        with patch("requests.request", return_value=mock_resp):
            status = adapter.health_check("a", {"server_id": 100})
        assert status == AgentStatus.STOPPED

    def test_health_deleting(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"server": {"status": "deleting"}}
        with patch("requests.request", return_value=mock_resp):
            status = adapter.health_check("a", {"server_id": 100})
        assert status == AgentStatus.STOPPED

    def test_health_initializing_is_degraded(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"server": {"status": "initializing"}}
        with patch("requests.request", return_value=mock_resp):
            status = adapter.health_check("a", {"server_id": 100})
        assert status == AgentStatus.DEGRADED

    def test_health_no_server_id_returns_stopped(self, adapter):
        assert adapter.health_check("a", {}) == AgentStatus.STOPPED

    def test_health_api_error_returns_failed(self, adapter):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "server error"
        mock_resp.json.return_value = {}
        with patch("requests.request", return_value=mock_resp):
            status = adapter.health_check("a", {"server_id": 100})
        assert status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# CloudProvider.provider_type property
# ---------------------------------------------------------------------------


class TestCloudProviderProviderType:
    def test_hetzner_provider_type(self):
        provider = CloudProvider(cloud="hetzner")
        assert provider.provider_type == ProviderType.HETZNER

    def test_aws_provider_type(self):
        provider = CloudProvider(cloud="aws")
        assert provider.provider_type == ProviderType.AWS

    def test_gcp_provider_type(self):
        provider = CloudProvider(cloud="gcp")
        assert provider.provider_type == ProviderType.GCP


# ---------------------------------------------------------------------------
# CloudProvider delegation to Hetzner
# ---------------------------------------------------------------------------


class TestCloudProviderHetznerDelegation:
    @pytest.fixture()
    def provider(self):
        return CloudProvider(cloud="hetzner", config={"api_token": "tok"})

    def test_provision_delegates_to_hetzner(self, provider):
        mock_result = {"server_id": 1, "host": "1.2.3.4", "container_id": "1"}
        with patch.object(provider._adapter, "provision", return_value=mock_result):
            result = provider.provision("a", _make_spec(), "t")
        assert result == mock_result

    def test_configure_delegates_to_hetzner(self, provider):
        with patch.object(provider._adapter, "configure", return_value=True):
            ok = provider.configure("a", _make_spec(), {})
        assert ok is True

    def test_start_delegates_to_hetzner(self, provider):
        with patch.object(provider._adapter, "start", return_value=True):
            ok = provider.start("a", {"server_id": 1})
        assert ok is True

    def test_stop_delegates_to_hetzner(self, provider):
        with patch.object(provider._adapter, "stop", return_value=True):
            ok = provider.stop("a", {"server_id": 1})
        assert ok is True

    def test_destroy_delegates_to_hetzner(self, provider):
        with patch.object(provider._adapter, "destroy", return_value=True):
            ok = provider.destroy("a", {"server_id": 1})
        assert ok is True

    def test_health_check_delegates_to_hetzner(self, provider):
        with patch.object(
            provider._adapter, "health_check", return_value=AgentStatus.RUNNING
        ):
            status = provider.health_check("a", {"server_id": 1})
        assert status == AgentStatus.RUNNING


# ---------------------------------------------------------------------------
# _build_cloud_init — Hetzner-specific tailscale authkey
# ---------------------------------------------------------------------------


class TestBuildCloudInitTailscale:
    def test_tailscale_join_included_when_authkey_set(self):
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec, tailscale_authkey="ts-key-abc")
        assert "ts-key-abc" in ci
        assert "tailscale up" in ci

    def test_tailscale_join_omitted_when_no_authkey(self):
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec, tailscale_authkey="")
        assert "tailscale up" not in ci or "--authkey" not in ci

    def test_tailscale_authkey_from_env(self, monkeypatch):
        monkeypatch.setenv("TAILSCALE_AUTHKEY", "env-ts-key")
        spec = _make_spec()
        ci = _build_cloud_init("agent", spec)
        assert "env-ts-key" in ci
