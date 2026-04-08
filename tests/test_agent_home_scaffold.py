"""Tests for fresh agent-home scaffolding."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from skcapstone.migrate_multi_agent import create_agent_home


class TestCreateAgentHome:
    def test_manifest_includes_human_operator_when_available(self, tmp_path: Path):
        """New agent homes persist the linked human operator in manifest.json."""
        with patch(
            "skcapstone.migrate_multi_agent.discover_human_operator",
            return_value={"name": "Casey", "fingerprint": "FP123", "relationship": "human-operator"},
        ):
            result = create_agent_home(tmp_path, "teddy")

        manifest = json.loads((tmp_path / "agents" / "teddy" / "manifest.json").read_text())
        assert result["agent_name"] == "teddy"
        assert manifest["name"] == "teddy"
        assert manifest["operator"]["name"] == "Casey"
        assert manifest["operator"]["fingerprint"] == "FP123"

    def test_manifest_omits_operator_when_none_available(self, tmp_path: Path):
        """New agent homes still create a valid manifest when no operator exists yet."""
        with patch("skcapstone.migrate_multi_agent.discover_human_operator", return_value=None):
            create_agent_home(tmp_path, "lumina")

        manifest = json.loads((tmp_path / "agents" / "lumina" / "manifest.json").read_text())
        assert manifest["name"] == "lumina"
        assert "operator" not in manifest
