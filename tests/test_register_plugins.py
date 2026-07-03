"""Tests for skskills-compiled plugin MCP server discovery.

Covers the pure `_discover_plugin_servers` function in skcapstone.register,
which scans `<workspace>/skskills/dist/*/.mcp.json` (the `skskills plugin
build` output) for MCP server envelopes. Deliberately does NOT call
`register_all` here — that function writes into the real `~/.claude` config
dirs and is out of scope for a unit test.
"""

from __future__ import annotations

import json

from skcapstone.register import _discover_plugin_servers


def test_discover_plugin_servers_finds_stdio_and_remote(tmp_path):
    plugin_dir = tmp_path / "skskills" / "dist" / "skcomms"
    plugin_dir.mkdir(parents=True)
    mcp_json = plugin_dir / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "skchat": {
                        "type": "stdio",
                        "command": "~/.skenv/bin/skchat-mcp",
                    },
                    "partner-remote": {
                        "type": "http",
                        "url": "http://x/mcp",
                    },
                }
            }
        )
    )

    servers = _discover_plugin_servers(tmp_path)

    assert len(servers) == 2

    by_name = {s["name"]: s for s in servers}

    stdio = by_name["skchat"]
    assert stdio["plugin"] == "skcomms"
    assert stdio["transport"] == "stdio"
    assert stdio["command"] == "~/.skenv/bin/skchat-mcp"
    assert stdio["url"] is None

    remote = by_name["partner-remote"]
    assert remote["plugin"] == "skcomms"
    assert remote["transport"] == "remote"
    assert remote["url"] == "http://x/mcp"
    assert remote["command"] is None


def test_discover_plugin_servers_missing_dist_returns_empty(tmp_path):
    # No skskills/dist directory at all.
    assert _discover_plugin_servers(tmp_path) == []

    # skskills/dist exists but is empty.
    (tmp_path / "skskills" / "dist").mkdir(parents=True)
    assert _discover_plugin_servers(tmp_path) == []
