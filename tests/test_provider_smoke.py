"""Provider import + basic-construct smoke test (coord 17c02209).

Guards the optional cloud/container provider backends against silent
breakage in CI.  The heavier per-provider suites
(``test_cloud_providers.py``, ``test_cloud_provider.py``,
``test_docker_provider.py``) exercise full mocked lifecycles; this file is a
minimal, always-green guard that every provider module *imports* and every
backend *constructs* without live cloud credentials, a Docker daemon, or
network access.

It also verifies the lazy cloud-SDK import path on both branches:

* When the SDK for an extra is installed (e.g. the ``cloud`` extra brings
  ``boto3`` / ``google-cloud-compute``), the client-factory reaches the real
  SDK and returns a client (the network call itself is mocked).
* When the SDK is *not* installed, the factory raises the guarded
  ``RuntimeError`` telling the operator which package to ``pip install``.

Nothing here needs credentials, a daemon, or the network, so it runs on a
clean runner for any single provider extra.
"""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import + registry
# ---------------------------------------------------------------------------


def test_provider_package_exports():
    """The providers package exposes all four backends."""
    providers = importlib.import_module("skcapstone.providers")
    for name in ("LocalProvider", "ProxmoxProvider", "CloudProvider", "DockerProvider"):
        assert hasattr(providers, name), f"missing export: {name}"


@pytest.mark.parametrize(
    "module",
    [
        "skcapstone.providers.local",
        "skcapstone.providers.proxmox",
        "skcapstone.providers.cloud",
        "skcapstone.providers.docker",
    ],
)
def test_provider_module_imports(module):
    """Every provider module imports without its optional SDK present."""
    assert importlib.import_module(module) is not None


# ---------------------------------------------------------------------------
# Basic construction (no SDK, no creds, no daemon)
# ---------------------------------------------------------------------------


def test_local_provider_constructs(tmp_path):
    from skcapstone.providers import LocalProvider

    provider = LocalProvider(home=tmp_path / "home", work_dir=tmp_path / "work")
    assert provider.provider_type is not None


def test_proxmox_provider_constructs():
    from skcapstone.providers import ProxmoxProvider

    provider = ProxmoxProvider(api_host="pve.example", token_name="t", token_value="v")
    assert provider.provider_type is not None


def test_docker_provider_constructs():
    from skcapstone.providers import DockerProvider

    provider = DockerProvider()
    assert provider.provider_type is not None


@pytest.mark.parametrize("cloud", ["hetzner", "aws", "gcp"])
def test_cloud_provider_constructs(cloud):
    """CloudProvider builds the matching adapter for each cloud (no SDK call)."""
    from skcapstone.blueprints.schema import ProviderType
    from skcapstone.providers import CloudProvider

    provider = CloudProvider(cloud=cloud)
    assert isinstance(provider.provider_type, ProviderType)


# ---------------------------------------------------------------------------
# Lazy SDK import path - installed branch (SDK returns a client)
# ---------------------------------------------------------------------------


def _sdk_available(module_path: str) -> bool:
    try:
        importlib.import_module(module_path)
        return True
    except ImportError:
        return False


def test_aws_client_path():
    """AWSAdapter._ec2_client reaches boto3 when installed, else guards."""
    from skcapstone.providers.cloud import AWSAdapter

    adapter = AWSAdapter(region="us-east-1")
    if _sdk_available("boto3"):
        with patch("boto3.client", return_value=MagicMock()) as mock_client:
            assert adapter._ec2_client() is not None
        mock_client.assert_called_once()
    else:
        with patch.dict("sys.modules", {"boto3": None}):
            with pytest.raises(RuntimeError, match="boto3"):
                adapter._ec2_client()


def test_gcp_client_path():
    """GCPAdapter._compute_client reaches compute_v1 when installed, else guards."""
    from skcapstone.providers.cloud import GCPAdapter

    adapter = GCPAdapter(project="demo")
    if _sdk_available("google.cloud.compute_v1"):
        with patch("google.cloud.compute_v1.InstancesClient", return_value=MagicMock()):
            assert adapter._compute_client() is not None
    else:
        with patch.dict("sys.modules", {"google.cloud.compute_v1": None}):
            with pytest.raises(RuntimeError, match="google-cloud-compute"):
                adapter._compute_client()


def test_docker_client_path():
    """DockerProvider._client reaches the docker SDK when installed, else guards."""
    from skcapstone.providers import DockerProvider

    provider = DockerProvider()
    if _sdk_available("docker"):
        mock_docker = MagicMock()
        mock_docker.from_env.return_value.ping.return_value = True
        with patch.dict("sys.modules", {"docker": mock_docker}):
            assert provider._client() is not None
    else:
        with patch.dict("sys.modules", {"docker": None}):
            with pytest.raises(RuntimeError, match="docker"):
                provider._client()
