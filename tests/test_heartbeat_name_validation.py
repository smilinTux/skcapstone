"""Path traversal rejection in heartbeat agent names (card 34006183 / F)."""

import pytest

from skcapstone.heartbeat import HeartbeatBeacon, validate_agent_name


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


class TestReadSideValidation:
    def _beacon(self, tmp_path):
        return HeartbeatBeacon(tmp_path, agent_name="lumina", heartbeats_dir=tmp_path / "hb")

    @pytest.mark.parametrize(
        "bad", ["../secret", "a/b", "..", " lumina", "lumina ", "x\x00y", "", None, 1, ["a"]]
    )
    def test_read_heartbeat_rejects_unsafe_names(self, tmp_path, bad):
        with pytest.raises((ValueError, TypeError, AttributeError)):
            self._beacon(tmp_path).read_heartbeat(bad)

    def test_read_heartbeat_does_not_read_outside_dir(self, tmp_path):
        (tmp_path / "secret.json").write_text("{}")
        (tmp_path / "hb").mkdir(exist_ok=True)
        with pytest.raises(ValueError):
            self._beacon(tmp_path).read_heartbeat("../secret")

    def test_valid_missing_name_returns_none(self, tmp_path):
        assert self._beacon(tmp_path).read_heartbeat("nobody") is None

    def test_whitespace_padded_name_not_silently_rewritten(self):
        with pytest.raises(ValueError, match="whitespace"):
            validate_agent_name(" lumina")
