"""
Infrastructure Providers - plug-in backends for agent deployment.

Each provider implements the ProviderBackend interface from team_engine.
The engine doesn't care where agents run; providers handle the details.
"""

from .cloud import CloudProvider
from .docker import DockerProvider
from .local import LocalProvider
from .proxmox import ProxmoxProvider

__all__ = ["LocalProvider", "ProxmoxProvider", "CloudProvider", "DockerProvider"]
