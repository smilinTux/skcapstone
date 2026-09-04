"""Path traversal rejection in heartbeat agent names (card 34006183 / F)."""

import pytest

from skcapstone.heartbeat import validate_agent_name


class TestValidateAgentName:
    def test_valid_names_pass(self):
        assert validate_agent_name("jarvis") == "jarvis"
        assert validate_agent_name("pi-codex-chiap01-worker") == "pi-codex-chiap01-worker"
        assert validate_agent_name("Agent-123") == "agent-123"  # lowercased

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_agent_name("")
        with pytest.raises(ValueError, match="non-empty"):
            validate_agent_name("   ")

    def test_path_traversal_rejected(self):
        for evil in ("../evil", "a/b", "..", "a/../../b", "./agent"):
            with pytest.raises(ValueError, match="path traversal"):
                validate_agent_name(evil)

    def test_special_characters_rejected(self):
        # The existing malformed file in the heartbeats dir is this gap
        for bad in ("agent: with spaces", "agent@test", "agent\nname", "a\\b"):
            with pytest.raises(ValueError, match="outside"):
                validate_agent_name(bad)

    def test_uppercase_normalised_not_rejected(self):
        # Uppercase is valid input, normalised to lowercase
        assert validate_agent_name("Jarvis") == "jarvis"
        assert validate_agent_name("PI-CODEX") == "pi-codex"

    def test_leading_dash_rejected(self):
        # Must start with [a-z0-9], not a dash
        with pytest.raises(ValueError):
            validate_agent_name("-agent")
        with pytest.raises(ValueError):
            validate_agent_name("---")
