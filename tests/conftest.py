"""Shared test fixtures for skcapstone."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_agent_home(tmp_path: Path) -> Path:
    """Provide a temporary agent home directory for testing."""
    agent_home = tmp_path / ".skcapstone"
    agent_home.mkdir()
    return agent_home


@pytest.fixture
def initialized_agent_home(tmp_agent_home: Path) -> Path:
    """Provide a fully initialized agent home with directory structure."""
    for subdir in ("identity", "memory", "trust", "security", "skills", "config", "sync"):
        (tmp_agent_home / subdir).mkdir()

    import json
    from datetime import datetime, timezone

    manifest = {
        "name": "test-agent",
        "version": "0.1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "connectors": [],
    }
    (tmp_agent_home / "manifest.json").write_text(json.dumps(manifest, indent=2))

    import yaml

    config = {"agent_name": "test-agent", "auto_rehydrate": True, "auto_audit": True}
    (tmp_agent_home / "config" / "config.yaml").write_text(
        yaml.dump(config, default_flow_style=False)
    )

    return tmp_agent_home
