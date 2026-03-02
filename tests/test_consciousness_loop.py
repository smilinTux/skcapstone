"""Tests for the consciousness loop — message classification, LLM bridge, system prompt."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.consciousness_loop import (
    ConsciousnessConfig,
    ConsciousnessLoop,
    LLMBridge,
    SystemPromptBuilder,
    _classify_message,
    _SimpleEnvelope,
    InboxHandler,
)
from skcapstone.model_router import TaskSignal
from skcapstone.blueprints.schema import ModelTier


class TestConsciousnessConfig:
    """ConsciousnessConfig Pydantic model tests."""

    def test_defaults(self):
        """Default config is sensible."""
        config = ConsciousnessConfig()
        assert config.enabled is True
        assert config.use_inotify is True
        assert config.max_concurrent_requests == 3
        assert "ollama" in config.fallback_chain
        assert "passthrough" in config.fallback_chain

    def test_custom_config(self):
        """Custom config overrides defaults."""
        config = ConsciousnessConfig(
            enabled=False,
            max_concurrent_requests=5,
            fallback_chain=["anthropic", "passthrough"],
        )
        assert config.enabled is False
        assert config.max_concurrent_requests == 5
        assert len(config.fallback_chain) == 2


class TestClassifyMessage:
    """Message classification tests."""

    def test_code_keywords(self):
        """Code-related messages get code tag."""
        signal = _classify_message("Please debug this function for me")
        assert "code" in signal.tags

    def test_analysis_keywords(self):
        """Analysis messages get analyze tag."""
        signal = _classify_message("Can you analyze this architecture?")
        assert "analyze" in signal.tags

    def test_simple_greeting(self):
        """Simple greetings get simple tag."""
        signal = _classify_message("hello")
        assert "simple" in signal.tags

    def test_general_message(self):
        """Messages with no keywords get general tag."""
        signal = _classify_message("The weather is nice today isn't it")
        assert "general" in signal.tags

    def test_token_estimation(self):
        """Token estimate is roughly content_length / 4."""
        msg = "a" * 400
        signal = _classify_message(msg)
        assert signal.estimated_tokens == 100

    def test_multi_tag(self):
        """Messages with multiple keyword sets get multiple tags."""
        signal = _classify_message("Can you debug and analyze this code?")
        assert "code" in signal.tags
        assert "analyze" in signal.tags


class TestLLMBridge:
    """LLM bridge routing and fallback tests."""

    def test_probe_passthrough_always_available(self):
        """Passthrough backend is always available."""
        config = ConsciousnessConfig()
        bridge = LLMBridge(config)
        assert bridge.available_backends.get("passthrough") is True

    def test_health_check_returns_dict(self):
        """Health check returns a dict of backend availability."""
        config = ConsciousnessConfig()
        bridge = LLMBridge(config)
        health = bridge.health_check()
        assert isinstance(health, dict)
        assert "passthrough" in health
        assert "ollama" in health

    @patch("skseed.llm.passthrough_callback")
    def test_generate_fallback_to_passthrough(self, mock_passthrough):
        """When no backends available, falls through to passthrough."""
        mock_cb = MagicMock(return_value="echo response")
        mock_passthrough.return_value = mock_cb

        config = ConsciousnessConfig(
            fallback_chain=["passthrough"],
        )
        bridge = LLMBridge(config)
        # Force all backends unavailable except passthrough
        bridge._available = {k: False for k in bridge._available}
        bridge._available["passthrough"] = True

        signal = TaskSignal(description="test", tags=["general"])
        result = bridge.generate("system", "hello", signal)
        # Should get a response (either from passthrough or last-resort message)
        assert isinstance(result, str)
        assert len(result) > 0

    @patch("skseed.llm.ollama_callback")
    def test_generate_passthrough_cascade_returns_user_content(self, mock_ollama):
        """When all LLM backends fail, cascade reaches passthrough and returns user content.

        Verifies the fallback cascade uses direct backend mapping (not _resolve_callback)
        so passthrough is reached without infinite regression, and that the returned
        value is the original user message — NOT the canned connectivity-error string.
        """
        from skcapstone.model_router import ModelRouterConfig

        # Ollama callback always raises — covers primary + alt model calls
        mock_ollama.return_value = MagicMock(side_effect=RuntimeError("ollama unavailable"))

        # Single model in FAST tier so there are no alt-model iterations,
        # and the tier-downgrade path is skipped (already FAST).
        router_cfg = ModelRouterConfig(
            tier_models={
                ModelTier.FAST.value: ["llama3.2"],
                ModelTier.CODE.value: ["devstral"],
                ModelTier.REASON.value: ["deepseek-r1:8b"],
                ModelTier.NUANCE.value: ["moonshot-v1-128k"],
                ModelTier.LOCAL.value: ["llama3.2"],
            },
            tag_rules=[],
        )
        config = ConsciousnessConfig(fallback_chain=["ollama", "passthrough"])
        bridge = LLMBridge(config, router_config=router_cfg)
        # All backends unavailable except passthrough
        bridge._available = {k: False for k in bridge._available}
        bridge._available["passthrough"] = True

        signal = TaskSignal(description="test", tags=["general"])
        result = bridge.generate("system prompt", "hello world", signal)

        assert result == "hello world", (
            f"Expected passthrough to return user message 'hello world', got: {result!r}"
        )
        assert "connectivity issues" not in result


class TestSystemPromptBuilder:
    """System prompt builder tests."""

    def test_build_with_empty_home(self, tmp_path):
        """Builder works even with empty home dir."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)
        prompt = builder.build()
        assert isinstance(prompt, str)
        # Should at least have behavioral instructions
        assert "Respond concisely" in prompt

    def test_build_with_identity(self, tmp_path):
        """Builder includes identity when present."""
        home = tmp_path / ".skcapstone"
        identity_dir = home / "identity"
        identity_dir.mkdir(parents=True)
        identity = {"name": "opus", "fingerprint": "ABCD1234"}
        (identity_dir / "identity.json").write_text(json.dumps(identity))

        builder = SystemPromptBuilder(home)
        prompt = builder.build()
        assert "opus" in prompt
        assert "ABCD1234" in prompt

    def test_conversation_history(self, tmp_path):
        """Builder tracks and includes per-peer conversation history."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Hello!")
        builder.add_to_history("jarvis", "assistant", "Hi there!")

        prompt = builder.build(peer_name="jarvis")
        assert "jarvis" in prompt
        assert "Hello!" in prompt

    def test_history_max_messages(self, tmp_path):
        """History is capped at max_messages per peer."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        for i in range(20):
            builder.add_to_history("peer", "user", f"Message {i}")

        # Default max is 10
        history = builder._conversation_history["peer"]
        assert len(history) == 10
        assert "Message 19" in history[-1]["content"]

    def test_truncation(self, tmp_path):
        """Long system prompts are truncated."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home, max_tokens=100)

        # Build should not exceed max_tokens * 4 chars
        prompt = builder.build()
        assert len(prompt) <= 100 * 4 + 50  # some slack for truncation marker

    def test_persistence_writes_json_file(self, tmp_path):
        """add_to_history writes a JSON file under {home}/conversations/{peer}.json."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Hello!")
        builder.add_to_history("jarvis", "assistant", "Hi there!")

        conv_file = home / "conversations" / "jarvis.json"
        assert conv_file.exists(), "Conversation file should be created"
        data = json.loads(conv_file.read_text())
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["role"] == "user"
        assert data[0]["content"] == "Hello!"
        assert data[1]["role"] == "assistant"

    def test_persistence_caps_at_max_history(self, tmp_path):
        """Persisted file is capped at max_history_messages."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home, max_history_messages=5)

        for i in range(8):
            builder.add_to_history("lumina", "user", f"Message {i}")

        conv_file = home / "conversations" / "lumina.json"
        data = json.loads(conv_file.read_text())
        assert len(data) == 5
        assert data[-1]["content"] == "Message 7"

    def test_load_existing_conversations_on_init(self, tmp_path):
        """Existing conversation files are loaded on __init__."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        conv_dir = home / "conversations"
        conv_dir.mkdir()

        history = [
            {"role": "user", "content": "Remembered message", "timestamp": "2026-01-01T00:00:00+00:00"},
        ]
        (conv_dir / "opus.json").write_text(json.dumps(history))

        builder = SystemPromptBuilder(home)
        assert "opus" in builder._conversation_history
        assert builder._conversation_history["opus"][0]["content"] == "Remembered message"

    def test_load_caps_at_max_history_on_init(self, tmp_path):
        """Loading from file caps history at max_history_messages."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        conv_dir = home / "conversations"
        conv_dir.mkdir()

        history = [
            {"role": "user", "content": f"Old message {i}", "timestamp": "2026-01-01T00:00:00+00:00"}
            for i in range(20)
        ]
        (conv_dir / "peer.json").write_text(json.dumps(history))

        builder = SystemPromptBuilder(home, max_history_messages=10)
        assert len(builder._conversation_history["peer"]) == 10
        assert builder._conversation_history["peer"][-1]["content"] == "Old message 19"

    def test_persistence_atomic_write(self, tmp_path):
        """No .tmp file left after write."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("ava", "user", "Test")
        tmp_file = home / "conversations" / "ava.json.tmp"
        assert not tmp_file.exists(), ".tmp file should not remain after atomic write"

    def test_multiple_peers_separate_files(self, tmp_path):
        """Each peer gets its own conversation file."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Hello from jarvis")
        builder.add_to_history("lumina", "user", "Hello from lumina")

        assert (home / "conversations" / "jarvis.json").exists()
        assert (home / "conversations" / "lumina.json").exists()
        jarvis_data = json.loads((home / "conversations" / "jarvis.json").read_text())
        lumina_data = json.loads((home / "conversations" / "lumina.json").read_text())
        assert jarvis_data[0]["content"] == "Hello from jarvis"
        assert lumina_data[0]["content"] == "Hello from lumina"


class TestSimpleEnvelope:
    """Test the minimal envelope for inotify-detected messages."""

    def test_parse_standard_format(self):
        """Standard SKComm envelope format parses correctly."""
        data = {
            "sender": "jarvis",
            "payload": {
                "content": "Hello from jarvis",
                "content_type": "text",
            },
        }
        env = _SimpleEnvelope(data)
        assert env.sender == "jarvis"
        assert env.payload.content == "Hello from jarvis"
        assert env.payload.content_type.value == "text"

    def test_parse_alt_format(self):
        """Alternative format with 'from' and 'message' keys."""
        data = {
            "from": "lumina",
            "message": "Hi!",
            "type": "text",
        }
        env = _SimpleEnvelope(data)
        assert env.sender == "lumina"
        assert env.payload.content == "Hi!"


class TestInboxHandler:
    """Inbox file handler debounce tests."""

    def test_skips_non_json(self):
        """Non-.skc.json files are ignored."""
        called = []
        handler = InboxHandler(lambda p: called.append(p))

        class FakeEvent:
            src_path = "/tmp/test.txt"
            is_directory = False

        handler.on_created(FakeEvent())
        assert len(called) == 0

    def test_processes_skc_json(self):
        """Valid .skc.json files are processed."""
        called = []
        handler = InboxHandler(lambda p: called.append(p), debounce_ms=0)

        class FakeEvent:
            src_path = "/tmp/inbox/peer/msg.skc.json"
            is_directory = False

        handler.on_created(FakeEvent())
        assert len(called) == 1

    def test_debounce(self):
        """Rapid duplicate events are debounced."""
        called = []
        handler = InboxHandler(lambda p: called.append(p), debounce_ms=5000)

        class FakeEvent:
            src_path = "/tmp/inbox/peer/msg.skc.json"
            is_directory = False

        handler.on_created(FakeEvent())
        handler.on_created(FakeEvent())  # Should be debounced
        assert len(called) == 1
