"""
Consciousness configuration loader.

Loads ConsciousnessConfig from YAML with override hierarchy:
    1. Per-agent config: {agent_home}/config/consciousness.yaml
    2. Environment: SKCAPSTONE_CONSCIOUSNESS_ENABLED=false
    3. CLI flag: --no-consciousness

Usage:
    config = load_consciousness_config(home)
    config = load_consciousness_config(home, cli_disabled=True)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

from .consciousness_loop import ConsciousnessConfig

logger = logging.getLogger("skcapstone.consciousness_config")

CONFIG_FILENAME = "consciousness.yaml"


def load_consciousness_config(
    home: Path,
    cli_disabled: bool = False,
    config_path: Optional[Path] = None,
) -> ConsciousnessConfig:
    """Load consciousness config with override hierarchy.

    Priority (highest wins):
        1. CLI flag (--no-consciousness)
        2. Environment variable (SKCAPSTONE_CONSCIOUSNESS_ENABLED)
        3. YAML config file
        4. Defaults

    Args:
        home: Agent home directory.
        cli_disabled: True if --no-consciousness was passed.
        config_path: Explicit path to config file (overrides default).

    Returns:
        Resolved ConsciousnessConfig.
    """
    config = ConsciousnessConfig()

    # Load from YAML
    yaml_path = config_path or (home / "config" / CONFIG_FILENAME)
    if yaml_path.exists():
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if raw and isinstance(raw, dict):
                config = ConsciousnessConfig.model_validate(raw)
                logger.info("Loaded consciousness config from %s", yaml_path)
        except Exception as exc:
            logger.warning(
                "Failed to parse %s, using defaults: %s", yaml_path, exc
            )

    # Environment override
    env_enabled = os.environ.get("SKCAPSTONE_CONSCIOUSNESS_ENABLED", "").lower()
    if env_enabled == "false":
        config.enabled = False
        logger.info("Consciousness disabled via SKCAPSTONE_CONSCIOUSNESS_ENABLED")
    elif env_enabled == "true":
        config.enabled = True

    # CLI override (highest priority)
    if cli_disabled:
        config.enabled = False
        logger.info("Consciousness disabled via --no-consciousness flag")

    return config


def write_default_config(home: Path, **overrides) -> Path:
    """Write the default consciousness config to disk.

    Creates {home}/config/consciousness.yaml with all defaults
    commented for reference.

    Args:
        home: Agent home directory.
        **overrides: Key-value overrides applied to the default config
            (e.g. ollama_host, ollama_model).

    Returns:
        Path to the created config file.
    """
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / CONFIG_FILENAME

    default = ConsciousnessConfig()
    if overrides:
        default = default.model_copy(update=overrides)
    content = yaml.dump(
        default.model_dump(),
        default_flow_style=False,
        sort_keys=False,
    )

    header = (
        "# Consciousness Loop Configuration\n"
        "#\n"
        "# Override hierarchy:\n"
        "#   1. CLI: skcapstone daemon start --no-consciousness\n"
        "#   2. Env: SKCAPSTONE_CONSCIOUSNESS_ENABLED=false\n"
        "#   3. This file\n"
        "#\n"
        "# See: skcapstone consciousness config --show\n"
        "\n"
    )

    config_path.write_text(header + content, encoding="utf-8")
    logger.info("Wrote default consciousness config to %s", config_path)
    return config_path


def load_dreaming_config(
    home: Path,
    config_path: Optional[Path] = None,
):
    """Load dreaming config from the consciousness.yaml ``dreaming:`` section.

    Args:
        home: Agent home directory.
        config_path: Explicit path to config file (overrides default).

    Returns:
        DreamingConfig (defaults if section is missing or unparseable).
    """
    from .dreaming import DreamingConfig

    yaml_path = config_path or (home / "config" / CONFIG_FILENAME)
    if not yaml_path.exists():
        return DreamingConfig()
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        if raw and isinstance(raw, dict) and "dreaming" in raw:
            return DreamingConfig.model_validate(raw["dreaming"])
    except Exception as exc:
        logger.warning("Failed to parse dreaming config: %s", exc)
    return DreamingConfig()
