"""Tests for the unified per-agent capability manifest (`skcapstone agent profile`)."""
from __future__ import annotations

import json

import yaml

from skcapstone.cli.agent_profile_cmd import (
    DEFAULT_BRIDGE_TOOLS,
    _resolved_bridge_tools,
    gather_profile,
)


def _make_agent_home(tmp_path, name: str = "tester", *, expose=None, soul_active=None):
    """Build a minimal agent home with soul + <agent>-mcp.yaml."""
    home = tmp_path / "agents" / name
    (home / "soul").mkdir(parents=True)
    (home / "config").mkdir(parents=True)
    (home / "soul" / "active.json").write_text(
        json.dumps(
            {
                "base_soul": name,
                "active_soul": soul_active or f"{name}-unhinged",
                "installed_souls": [f"{name}-unhinged", name],
            }
        )
    )
    servers = {
        "skmemory": {
            "command": "/bin/true",
            "enabled": True,
            "expose_tools": expose if expose is not None else ["memory_search", "memory_store"],
        },
        "disabled_one": {"command": "/bin/true", "enabled": False, "expose_tools": ["nope"]},
    }
    (home / "config" / f"{name}-mcp.yaml").write_text(yaml.dump({"servers": servers}))
    return home


def test_gather_profile_happy_path(tmp_path):
    """Manifest reflects soul, only-enabled servers, and exposed tools."""
    home = _make_agent_home(tmp_path)
    m = gather_profile(home, "tester")

    assert m["agent"] == "tester"
    assert m["soul"]["active"] == "tester-unhinged"
    assert m["soul"]["base"] == "tester"
    # disabled server is excluded; enabled one is present
    assert "skmemory" in m["mcp"]["servers"]
    assert "disabled_one" not in m["mcp"]["servers"]
    assert m["mcp"]["exposed_tools"] == ["memory_search", "memory_store"]
    # default bridge curation when no profile.yaml present
    assert m["bridge"]["tools"] == "default"
    assert m["bridge"]["voice_reply"] == "voice"


def test_resolved_tools_intersects_default_with_allowed(tmp_path):
    """The default curation is intersected with the server allow-list."""
    home = _make_agent_home(tmp_path, expose=["memory_search", "coord_status", "not_in_default"])
    m = gather_profile(home, "tester")
    resolved = _resolved_bridge_tools(m)

    # only tools that are BOTH in the curated default AND exposed survive
    assert "memory_search" in resolved
    assert "coord_status" in resolved
    assert "not_in_default" not in resolved  # exposed but not in curated default
    assert all(t in DEFAULT_BRIDGE_TOOLS for t in resolved)


def test_resolved_tools_all_and_explicit(tmp_path):
    """tools: 'all' returns every exposed tool; an explicit list is honored verbatim."""
    home = _make_agent_home(tmp_path, expose=["memory_search", "coord_status"])

    m_all = gather_profile(home, "tester")
    m_all["bridge"]["tools"] = "all"
    assert sorted(_resolved_bridge_tools(m_all)) == ["coord_status", "memory_search"]

    m_list = gather_profile(home, "tester")
    m_list["bridge"]["tools"] = ["just_this_one"]
    assert _resolved_bridge_tools(m_list) == ["just_this_one"]


def test_profile_yaml_bridge_block_is_read(tmp_path):
    """A profile.yaml bridge block overrides the curation defaults."""
    home = _make_agent_home(tmp_path)
    (home / "profile.yaml").write_text(
        yaml.dump({"agent": "tester", "bridge": {"tools": "all", "voice_reply": "off"}})
    )
    m = gather_profile(home, "tester")
    assert m["bridge"]["tools"] == "all"
    assert m["bridge"]["voice_reply"] == "off"
