"""
Blueprint Registry — discovers, validates, and loads team blueprints.

Searches three locations in priority order:
1. User blueprints:  ~/.skcapstone/blueprints/teams/
2. Vault blueprints: ~/.skcapstone/vaults/blueprints/teams/  (synced via skref)
3. Built-in blueprints: shipped with the skcapstone package

This follows the filesystem-context skill pattern: use the filesystem
as the source of truth, keep it simple, add sophistication later.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from .schema import BlueprintManifest

logger = logging.getLogger(__name__)

# Built-in blueprints ship alongside this module
_BUILTIN_DIR = Path(__file__).parent / "builtins"


class BlueprintRegistry:
    """Discovers and manages agent team blueprints.

    Args:
        home: Agent home directory (default ~/.skcapstone).
    """

    def __init__(self, home: Optional[Path] = None) -> None:
        self._home = (home or Path("~/.skcapstone")).expanduser()
        self._cache: Dict[str, BlueprintManifest] = {}

    # ------------------------------------------------------------------
    # Discovery paths
    # ------------------------------------------------------------------

    @property
    def _search_paths(self) -> List[Path]:
        """Return blueprint directories in priority order."""
        return [
            self._home / "blueprints" / "teams",
            self._home / "vaults" / "blueprints" / "teams",
            _BUILTIN_DIR,
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> Dict[str, BlueprintManifest]:
        """Scan all search paths and return discovered blueprints.

        Later paths lose to earlier paths when slugs collide (user
        overrides take priority over built-ins).

        Returns:
            Dict mapping slug to validated BlueprintManifest.
        """
        found: Dict[str, BlueprintManifest] = {}

        # Reverse so built-ins load first, then get overridden
        for search_dir in reversed(self._search_paths):
            if not search_dir.is_dir():
                continue
            for yaml_file in sorted(search_dir.glob("*.yaml")):
                try:
                    bp = self._load_file(yaml_file)
                    found[bp.slug] = bp
                except Exception as exc:
                    logger.warning("Skipping %s: %s", yaml_file, exc)

            for yml_file in sorted(search_dir.glob("*.yml")):
                try:
                    bp = self._load_file(yml_file)
                    found[bp.slug] = bp
                except Exception as exc:
                    logger.warning("Skipping %s: %s", yml_file, exc)

        self._cache = found
        return found

    def list_blueprints(self) -> List[BlueprintManifest]:
        """Return all discovered blueprints as a sorted list.

        Returns:
            List of BlueprintManifest objects sorted by name.
        """
        if not self._cache:
            self.scan()
        return sorted(self._cache.values(), key=lambda b: b.name)

    def get(self, slug: str) -> Optional[BlueprintManifest]:
        """Get a specific blueprint by slug.

        Args:
            slug: The blueprint identifier.

        Returns:
            BlueprintManifest or None if not found.
        """
        if not self._cache:
            self.scan()
        return self._cache.get(slug)

    def save_blueprint(
        self,
        blueprint: BlueprintManifest,
        location: str = "user",
    ) -> Path:
        """Save a blueprint to the user or vault directory.

        Args:
            blueprint: The validated blueprint to save.
            location: 'user' for ~/.skcapstone/blueprints/teams/,
                      'vault' for ~/.skcapstone/vaults/blueprints/teams/.

        Returns:
            Path to the written file.
        """
        if location == "vault":
            target_dir = self._home / "vaults" / "blueprints" / "teams"
        else:
            target_dir = self._home / "blueprints" / "teams"

        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / f"{blueprint.slug}.yaml"

        data = blueprint.model_dump(mode="json", exclude_none=True)
        target_file.write_text(
            yaml.dump(data, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )

        self._cache[blueprint.slug] = blueprint
        return target_file

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _load_file(path: Path) -> BlueprintManifest:
        """Parse and validate a single blueprint YAML file.

        Args:
            path: Path to the YAML file.

        Returns:
            Validated BlueprintManifest.

        Raises:
            ValueError: If the YAML is invalid or fails validation.
        """
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"Expected a YAML mapping, got {type(raw).__name__}")
        return BlueprintManifest(**raw)
