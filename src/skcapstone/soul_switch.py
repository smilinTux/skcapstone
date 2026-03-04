"""
Soul Switch — load and activate named soul personas.

Loads soul blueprints from ``~/.skcapstone/souls/{name}.yaml`` and
persists the active selection to ``~/.skcapstone/souls/active.json``.

A switch-soul blueprint is a YAML file with three soul-specific fields
on top of the standard SoulBlueprint ones:

    name:           slug used to reference this soul (e.g. "lumina")
    display_name:   human-readable label
    agent_name:     name used in desktop notifications (defaults to display_name)
    system_prompt:  full consciousness system-prompt override injected by
                    SystemPromptBuilder when this soul is active
    journal_tone:   short string describing the emotional tone for journal entries

All fields except ``name`` are optional — an empty blueprint still
records the soul as active and suppresses the default personality text.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger("skcapstone.soul_switch")

# Subdirectory inside agent home that holds switch-soul blueprints.
_SOULS_DIR = "souls"
_ACTIVE_FILE = "souls/active.json"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SoulSwitchBlueprint(BaseModel):
    """A named soul persona blueprint loaded from ~/.skcapstone/souls/{name}.yaml."""

    name: str
    display_name: str = ""
    agent_name: str = ""
    system_prompt: str = ""
    journal_tone: str = ""
    # Optional descriptive extras (mirroring SoulBlueprint vocabulary)
    category: str = "custom"
    vibe: str = ""
    core_traits: list[str] = Field(default_factory=list)

    def effective_agent_name(self) -> str:
        """Return agent_name, falling back to display_name, then name."""
        return self.agent_name or self.display_name or self.name

    def to_system_prompt_section(self) -> str:
        """Build the text injected into the consciousness system prompt."""
        if self.system_prompt:
            return self.system_prompt

        # Minimal fallback when no system_prompt is authored
        parts: list[str] = [f"Active soul: {self.effective_agent_name()}"]
        if self.vibe:
            parts.append(f"Vibe: {self.vibe}")
        if self.core_traits:
            parts.append(f"Traits: {', '.join(self.core_traits)}")
        return "\n".join(parts)


class SoulSwitchState(BaseModel):
    """Persisted record of which switch-soul is currently active."""

    active: Optional[str] = None
    activated_at: Optional[str] = None
    previous: Optional[str] = None


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _souls_dir(home: Path) -> Path:
    d = home / _SOULS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(home: Path) -> Path:
    return home / _ACTIVE_FILE


def _load_state(home: Path) -> SoulSwitchState:
    path = _state_path(home)
    if not path.exists():
        return SoulSwitchState()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return SoulSwitchState.model_validate(data)
    except Exception:
        return SoulSwitchState()


def _save_state(home: Path, state: SoulSwitchState) -> None:
    _souls_dir(home)  # ensure dir exists
    path = _state_path(home)
    path.write_text(state.model_dump_json(indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_switch_soul(home: Path, name: str) -> SoulSwitchBlueprint:
    """Load a named soul blueprint from ``~/.skcapstone/souls/{name}.yaml``.

    Args:
        home:  Agent home directory (``~/.skcapstone``).
        name:  Soul slug (filename stem, no extension).

    Returns:
        Parsed :class:`SoulSwitchBlueprint`.

    Raises:
        FileNotFoundError: Blueprint file does not exist.
        ValueError: YAML is invalid or cannot be coerced into the model.
    """
    blueprint_path = _souls_dir(home) / f"{name}.yaml"
    if not blueprint_path.exists():
        raise FileNotFoundError(
            f"Soul blueprint not found: {blueprint_path}\n"
            f"Create it at ~/.skcapstone/souls/{name}.yaml"
        )

    raw = blueprint_path.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {blueprint_path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"Expected a YAML mapping in {blueprint_path}, got {type(data).__name__}"
        )

    # Inject the name from filename if not present in file
    data.setdefault("name", name)

    # Coerce None → "" for string fields
    for str_field in ("display_name", "agent_name", "system_prompt", "journal_tone",
                      "category", "vibe"):
        if data.get(str_field) is None:
            data[str_field] = ""

    try:
        return SoulSwitchBlueprint.model_validate(data)
    except Exception as exc:
        raise ValueError(f"Invalid soul blueprint in {blueprint_path}: {exc}") from exc


def set_active_switch(home: Path, name: str) -> SoulSwitchBlueprint:
    """Activate a named soul and persist the selection.

    Args:
        home:  Agent home directory.
        name:  Soul slug to activate.

    Returns:
        The loaded :class:`SoulSwitchBlueprint`.

    Raises:
        FileNotFoundError: Blueprint not found.
        ValueError: Blueprint YAML is invalid.
    """
    blueprint = load_switch_soul(home, name)
    state = _load_state(home)
    new_state = SoulSwitchState(
        active=name,
        activated_at=datetime.now(timezone.utc).isoformat(),
        previous=state.active,
    )
    _save_state(home, new_state)
    logger.info("Soul switched to '%s' (agent_name=%r)", name, blueprint.effective_agent_name())
    return blueprint


def clear_active_switch(home: Path) -> None:
    """Deactivate the current switch-soul, returning to base identity.

    Args:
        home: Agent home directory.
    """
    state = _load_state(home)
    new_state = SoulSwitchState(
        active=None,
        activated_at=None,
        previous=state.active,
    )
    _save_state(home, new_state)
    logger.info("Soul switch cleared (was: %r)", state.active)


def get_active_switch_state(home: Path) -> SoulSwitchState:
    """Return the current switch state (which soul is active, if any).

    Args:
        home: Agent home directory.

    Returns:
        :class:`SoulSwitchState` — ``active`` is ``None`` when at base.
    """
    return _load_state(home)


def get_active_switch_blueprint(home: Path) -> Optional[SoulSwitchBlueprint]:
    """Return the currently active switch-soul blueprint, or ``None``.

    Silently returns ``None`` on any read/parse error so callers
    can fall through to base identity without crashing.

    Args:
        home: Agent home directory.

    Returns:
        :class:`SoulSwitchBlueprint` or ``None`` if no soul is active.
    """
    state = _load_state(home)
    if not state.active:
        return None
    try:
        return load_switch_soul(home, state.active)
    except Exception as exc:
        logger.debug("Could not load active switch soul '%s': %s", state.active, exc)
        return None


def list_available_souls(home: Path) -> list[str]:
    """Return the slug names of all available soul blueprints.

    Args:
        home: Agent home directory.

    Returns:
        Sorted list of slug names (file stems of ``.yaml`` files).
    """
    souls_dir = home / _SOULS_DIR
    if not souls_dir.exists():
        return []
    return sorted(p.stem for p in souls_dir.glob("*.yaml"))
