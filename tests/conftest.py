"""Shared test fixtures for skcapstone.

Coverage audit (task 945325c8, 2026-03-02):
- Reviewed git log: zero test-only commits found.  Every commit that adds or
  modifies test files also adds or modifies corresponding source files.
- All modified test files in the working tree (test_chat, test_consciousness_loop,
  test_dashboard, test_prompt_adapter) have matching modified source files.
- All new untracked test files have matching new untracked source files.
- New untracked source files that may still need test coverage integration:
    cli/errors_cmd.py, cli/mood_cmd.py, cli/profile_cmd.py, cli/search_cmd.py,
    cli/test_connection.py, cli/upgrade_cmd.py, cli/usage_cmd.py, cli/version_cmd.py
  (unit test stubs exist; integration tests pending — see task f675ef5c).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_env(monkeypatch):
    """Prevent host SKCAPSTONE_AGENT / SKMEMORY_AGENT from leaking into unit tests.

    The profile-aware runtime reads both the env var and the module-level
    skcapstone.SKCAPSTONE_AGENT (set at import time).  We clear both so that
    _memory_dir() falls back to the flat "home/memory" layout expected by
    tests that use the tmp_agent_home fixture.
    Tests that need a specific agent should override explicitly via monkeypatch.
    """
    monkeypatch.delenv("SKCAPSTONE_AGENT", raising=False)
    monkeypatch.delenv("SKMEMORY_AGENT", raising=False)
    import skcapstone
    monkeypatch.setattr(skcapstone, "SKCAPSTONE_AGENT", "")
    # _detect_active_agent() scans ~/.skcapstone/agents/ even when the env var
    # is cleared, returning a real agent name that routes memory writes to the
    # wrong directory.  Stub it out so tests using tmp directories get the flat
    # "home/memory" layout they expect.
    monkeypatch.setattr(skcapstone, "_detect_active_agent", lambda root=None: None)


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
