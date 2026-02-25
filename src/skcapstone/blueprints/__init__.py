"""
Agent Team Blueprints — selectable, deployable AI workforces.

The First Sovereign Singularity in History.
Brought to you by the Kings and Queens of smilinTux.org
"""

from .schema import AgentSpec, BlueprintManifest, NetworkConfig, StorageConfig
from .registry import BlueprintRegistry

__all__ = [
    "AgentSpec",
    "BlueprintManifest",
    "NetworkConfig",
    "StorageConfig",
    "BlueprintRegistry",
]
