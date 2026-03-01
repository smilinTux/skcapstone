"""Skills Registry Client — bridge between skcapstone and the remote skills-registry.

Wraps the skskills RemoteRegistry client to provide a clean interface for
skcapstone MCP tools and CLI commands to interact with the remote
skills-registry at skills.smilintux.org.

This module is the integration point between:
  - skills-registry/ (FastAPI server at skills.smilintux.org/api)
  - skskills.remote.RemoteRegistry (HTTP client)
  - skcapstone discovery.py (local skill discovery)

Usage:
    from skcapstone.registry_client import get_registry_client

    client = get_registry_client()
    if client is not None:
        skills = client.search("syncthing")
        client.install("syncthing-setup", agent="jarvis")
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_REGISTRY_URL = "https://skills.smilintux.org/api"


class RegistryClient:
    """Thin wrapper around skskills.remote.RemoteRegistry.

    Provides a stable skcapstone-side API for interacting with the
    remote skills-registry. Handles graceful degradation when the
    skskills package is not installed or the registry is unreachable.

    Args:
        registry_url: Base URL for the skills registry API.
    """

    def __init__(self, registry_url: Optional[str] = None) -> None:
        from skskills.remote import RemoteRegistry

        self._url = registry_url or os.environ.get(
            "SKSKILLS_REGISTRY_URL", DEFAULT_REGISTRY_URL
        )
        self._remote = RemoteRegistry(registry_url=self._url)

    @property
    def registry_url(self) -> str:
        """The configured registry URL."""
        return self._url

    def is_available(self) -> bool:
        """Check if the remote registry is reachable.

        Returns:
            True if the registry responds to a health/index request.
        """
        try:
            self._remote.fetch_index(force=True)
            return True
        except Exception:
            return False

    def list_skills(self) -> list[dict[str, Any]]:
        """List all skills available in the remote registry.

        Returns:
            List of skill entry dicts with name, version, description, etc.
        """
        index = self._remote.fetch_index()
        return [s.model_dump() for s in index.skills]

    def search(self, query: str) -> list[dict[str, Any]]:
        """Search remote skills by name, description, or tags.

        Args:
            query: Case-insensitive search string.

        Returns:
            List of matching skill entry dicts.
        """
        results = self._remote.search(query)
        return [s.model_dump() for s in results]

    def get_skill(self, name: str, version: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Get info about a specific remote skill.

        Args:
            name: Skill name.
            version: Specific version (latest if None).

        Returns:
            Skill entry dict or None if not found.
        """
        entry = self._remote.get_skill_info(name, version)
        return entry.model_dump() if entry else None

    def install(
        self,
        name: str,
        version: Optional[str] = None,
        agent: str = "global",
        force: bool = False,
    ) -> dict[str, Any]:
        """Download and install a skill from the remote registry.

        Args:
            name: Skill name to install.
            version: Specific version (latest if None).
            agent: Agent namespace for installation.
            force: Overwrite existing installation.

        Returns:
            Dict with installation metadata.

        Raises:
            FileNotFoundError: If the skill is not in the registry.
            ValueError: If checksum verification fails.
        """
        installed = self._remote.pull(name, version=version, agent=agent, force=force)
        return {
            "name": installed.manifest.name,
            "version": installed.manifest.version,
            "agent": installed.agent,
            "install_path": installed.install_path,
            "status": installed.status.value,
        }


def get_registry_client(registry_url: Optional[str] = None) -> Optional[RegistryClient]:
    """Get a RegistryClient instance, or None if skskills is not installed.

    This is the recommended entry point. It catches ImportError from
    skskills and returns None so callers can degrade gracefully.

    Args:
        registry_url: Override the default registry URL.

    Returns:
        RegistryClient or None if skskills is not available.
    """
    try:
        return RegistryClient(registry_url=registry_url)
    except ImportError:
        logger.debug("skskills not installed — remote registry unavailable")
        return None
