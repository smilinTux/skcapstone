"""Tests for human-operator manifest linking."""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.operator_link import build_agent_manifest, discover_human_operator


def test_discover_human_operator_reads_capauth_profile(tmp_path: Path) -> None:
    """A CapAuth human profile is converted into operator metadata."""
    capauth_home = tmp_path / ".capauth"
    profile = capauth_home / "identity" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "entity": {
                    "name": "Casey",
                    "entity_type": "human",
                    "email": "casey@example.com",
                    "handle": "casey@example.com",
                },
                "key_info": {
                    "fingerprint": "ABCDEF1234567890",
                },
            }
        ),
        encoding="utf-8",
    )

    operator = discover_human_operator(capauth_home)

    assert operator == {
        "name": "Casey",
        "relationship": "human-operator",
        "entity_type": "human",
        "source": "capauth",
        "email": "casey@example.com",
        "handle": "casey@example.com",
        "fingerprint": "ABCDEF1234567890",
    }


def test_discover_human_operator_ignores_non_human_profile(tmp_path: Path) -> None:
    """AI CapAuth profiles are not treated as human operators."""
    capauth_home = tmp_path / ".capauth"
    profile = capauth_home / "identity" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        json.dumps(
            {
                "entity": {
                    "name": "Jarvis",
                    "entity_type": "ai",
                }
            }
        ),
        encoding="utf-8",
    )

    assert discover_human_operator(capauth_home) is None


def test_build_agent_manifest_includes_operator_when_available() -> None:
    """Operator metadata is persisted directly in the manifest."""
    manifest = build_agent_manifest(
        "jarvis",
        "0.6.0",
        created_at="2026-01-01T00:00:00+00:00",
        operator={"name": "Casey", "fingerprint": "FP123", "relationship": "human-operator"},
    )

    assert manifest["name"] == "jarvis"
    assert manifest["entity_type"] == "ai-agent"
    assert manifest["operator"]["name"] == "Casey"
    assert manifest["operator"]["fingerprint"] == "FP123"
