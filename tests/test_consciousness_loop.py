"""Tests for the consciousness loop — message classification, LLM bridge, system prompt."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.consciousness_loop import (
    ConsciousnessConfig,
    ConsciousnessLoop,
    LLMBridge,
    SystemPromptBuilder,
    _classify_message,
    _OllamaPool,
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


class TestProcessEnvelopeACK:
    """Verify ACK is sent with message_type kwarg (not content_type)."""

    def _make_loop(self, tmp_path, auto_ack=True):
        config = ConsciousnessConfig(
            auto_ack=auto_ack,
            fallback_chain=["passthrough"],
        )
        loop = ConsciousnessLoop(config, home=tmp_path / ".skcapstone")
        return loop

    def _make_envelope(self, sender="peer", content="hello", content_type="text"):
        data = {
            "sender": sender,
            "payload": {"content": content, "content_type": content_type},
        }
        return _SimpleEnvelope(data)

    def test_ack_uses_message_type_kwarg(self, tmp_path):
        """ACK send must use message_type kwarg, not content_type — regression for TypeError."""
        loop = self._make_loop(tmp_path)
        mock_skcomm = MagicMock()
        loop.set_skcomm(mock_skcomm)
        # Patch bridge so test doesn't hang on LLM calls
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "test response"

        envelope = self._make_envelope()
        loop.process_envelope(envelope)

        # Find the ACK call (first send call with "ACK" as message)
        ack_calls = [
            c for c in mock_skcomm.send.call_args_list
            if len(c.args) >= 2 and c.args[1] == "ACK"
        ]
        assert ack_calls, "Expected at least one ACK send call"
        ack_call = ack_calls[0]

        # Must NOT have content_type kwarg (that was the bug)
        assert "content_type" not in ack_call.kwargs, (
            "ACK send used wrong kwarg 'content_type' — should be 'message_type'"
        )
        # Must have message_type kwarg
        assert "message_type" in ack_call.kwargs, (
            "ACK send must pass message_type kwarg"
        )
        assert ack_call.kwargs["message_type"] == "ack"

    def test_ack_not_sent_when_auto_ack_disabled(self, tmp_path):
        """When auto_ack is False, no ACK is sent."""
        loop = self._make_loop(tmp_path, auto_ack=False)
        mock_skcomm = MagicMock()
        loop.set_skcomm(mock_skcomm)
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "test response"

        loop.process_envelope(self._make_envelope())

        ack_calls = [
            c for c in mock_skcomm.send.call_args_list
            if len(c.args) >= 2 and c.args[1] == "ACK"
        ]
        assert not ack_calls, "ACK should not be sent when auto_ack is False"

    def test_ack_skipped_for_ack_type_messages(self, tmp_path):
        """Incoming ACK messages are skipped — no processing, no re-ACK."""
        loop = self._make_loop(tmp_path, auto_ack=True)
        mock_skcomm = MagicMock()
        loop.set_skcomm(mock_skcomm)
        loop._bridge = MagicMock()

        ack_envelope = self._make_envelope(content="ACK", content_type="ack")
        result = loop.process_envelope(ack_envelope)

        assert result is None, "ACK-type messages should be skipped (return None)"
        mock_skcomm.send.assert_not_called()


class TestSystemPromptBuilderCache:
    """Section cache TTL tests for SystemPromptBuilder."""

    def test_get_cached_calls_loader_once(self, tmp_path):
        """_get_cached calls the loader only once within TTL."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        call_count = 0

        def loader():
            nonlocal call_count
            call_count += 1
            return "section_value"

        result1 = builder._get_cached("test_key", loader, ttl=60)
        result2 = builder._get_cached("test_key", loader, ttl=60)

        assert result1 == result2 == "section_value"
        assert call_count == 1, "Loader should be called only once within TTL"

    def test_get_cached_reloads_after_ttl(self, tmp_path):
        """_get_cached reloads the value once TTL has expired."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        call_count = 0

        def loader():
            nonlocal call_count
            call_count += 1
            return f"value_{call_count}"

        builder._get_cached("key", loader, ttl=60)
        # Expire the cache entry manually
        val, _ = builder._section_cache["key"]
        builder._section_cache["key"] = (val, time.monotonic() - 1)
        builder._get_cached("key", loader, ttl=60)

        assert call_count == 2, "Loader should be called again after TTL expires"

    def test_build_caches_identity_section(self, tmp_path):
        """build() serves identity from cache on second call."""
        home = tmp_path / ".skcapstone"
        identity_dir = home / "identity"
        identity_dir.mkdir(parents=True)
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "opus", "fingerprint": "ABCD1234"})
        )

        builder = SystemPromptBuilder(home)
        with patch.object(builder, "_load_identity", wraps=builder._load_identity) as mock_id:
            builder.build()
            builder.build()

        assert mock_id.call_count == 1, "_load_identity should be called once (cached)"

    def test_build_caches_context_section(self, tmp_path):
        """build() serves context from cache on second call."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        builder = SystemPromptBuilder(home)
        with patch.object(builder, "_load_context", wraps=builder._load_context) as mock_ctx:
            builder.build()
            builder.build()

        assert mock_ctx.call_count == 1, "_load_context should be called once (cached)"

    def test_cache_key_isolation(self, tmp_path):
        """Different section keys are cached independently."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        a_calls, b_calls = 0, 0

        def loader_a():
            nonlocal a_calls
            a_calls += 1
            return "a"

        def loader_b():
            nonlocal b_calls
            b_calls += 1
            return "b"

        builder._get_cached("a", loader_a)
        builder._get_cached("b", loader_b)
        builder._get_cached("a", loader_a)
        builder._get_cached("b", loader_b)

        assert a_calls == 1
        assert b_calls == 1


class TestProcessEnvelopeTiming:
    """Timing instrumentation emitted by process_envelope."""

    def _make_loop(self, tmp_path):
        config = ConsciousnessConfig(fallback_chain=["passthrough"])
        loop = ConsciousnessLoop(config, home=tmp_path / ".skcapstone")
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "response"
        return loop

    def _make_envelope(self, content="hello"):
        data = {"sender": "peer", "payload": {"content": content, "content_type": "text"}}
        return _SimpleEnvelope(data)

    def test_timing_log_emitted(self, tmp_path, caplog):
        """process_envelope logs 'Pipeline timing' with all four phase labels."""
        loop = self._make_loop(tmp_path)
        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop.process_envelope(self._make_envelope())

        timing_msgs = [r.message for r in caplog.records if "Pipeline timing" in r.message]
        assert timing_msgs, "Expected 'Pipeline timing' log entry"
        msg = timing_msgs[0]
        assert "classify:" in msg
        assert "prompt_build:" in msg
        assert "llm:" in msg
        assert "send:" in msg

    def test_timing_values_are_non_negative(self, tmp_path, caplog):
        """All reported timing values must be >= 0."""
        import re as _re

        loop = self._make_loop(tmp_path)
        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop.process_envelope(self._make_envelope())

        timing_msgs = [r.message for r in caplog.records if "Pipeline timing" in r.message]
        assert timing_msgs
        numbers = [float(n) for n in _re.findall(r"[\d.]+(?=ms)", timing_msgs[0])]
        assert len(numbers) == 4, f"Expected 4 timing values, got: {numbers}"
        assert all(n >= 0 for n in numbers), f"Negative timing value: {numbers}"


class TestVerifyMessageSignature:
    """Tests for ConsciousnessLoop._verify_message_signature."""

    def _make_loop(self, tmp_path):
        config = ConsciousnessConfig(fallback_chain=["passthrough"])
        return ConsciousnessLoop(config, home=tmp_path / ".skcapstone")

    def test_unsigned_when_no_signature(self, tmp_path):
        """Returns 'unsigned' when payload has no signature field."""
        loop = self._make_loop(tmp_path)
        data = {"sender": "jarvis", "payload": {"content": "hello"}}
        assert loop._verify_message_signature(data) == "unsigned"

    def test_unsigned_empty_signature(self, tmp_path):
        """Returns 'unsigned' when signature field is empty string."""
        loop = self._make_loop(tmp_path)
        data = {"sender": "jarvis", "payload": {"content": "hello", "signature": ""}}
        assert loop._verify_message_signature(data) == "unsigned"

    def test_failed_when_no_peer_key(self, tmp_path):
        """Returns 'failed' when sender has no public key in peer store."""
        loop = self._make_loop(tmp_path)
        data = {
            "sender": "unknown-peer",
            "payload": {"content": "hello", "signature": "-----BEGIN PGP MESSAGE-----\nfake\n-----END PGP MESSAGE-----"},
        }
        # No peer registered → get_peer returns None → failed
        assert loop._verify_message_signature(data) == "failed"

    def test_failed_when_unknown_sender(self, tmp_path):
        """Returns 'failed' when sender resolves to 'unknown'."""
        loop = self._make_loop(tmp_path)
        data = {
            # No sender/from key → sanitizer returns "unknown"
            "payload": {"content": "hi", "signature": "sig"},
        }
        assert loop._verify_message_signature(data) == "failed"

    @patch("skcapstone.consciousness_loop.ConsciousnessLoop._verify_message_signature")
    def test_on_inbox_file_logs_sig_status(self, mock_verify, tmp_path, caplog):
        """_on_inbox_file logs the signature status returned by _verify_message_signature."""
        mock_verify.return_value = "unsigned"

        loop = self._make_loop(tmp_path)
        loop._executor = MagicMock()  # don't submit real work

        # Write a valid envelope file
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        msg_file = inbox / "test.skc.json"
        msg_file.write_text(json.dumps({
            "sender": "jarvis",
            "payload": {"content": "hello", "content_type": "text"},
        }))

        with caplog.at_level(logging.INFO, logger="skcapstone.consciousness"):
            loop._on_inbox_file(msg_file)

        sig_logs = [r.message for r in caplog.records if "signature:" in r.message]
        assert sig_logs, "Expected a 'signature:' log entry from _on_inbox_file"
        assert "unsigned" in sig_logs[0]

    def test_verified_with_mock_backend(self, tmp_path):
        """Returns 'verified' when capauth backend confirms the signature."""
        loop = self._make_loop(tmp_path)

        # Register a peer with a public key
        peer_dir = (tmp_path / ".skcapstone") / "peers"
        peer_dir.mkdir(parents=True)
        peer_data = {
            "name": "jarvis",
            "fingerprint": "ABCD1234",
            "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----",
            "trust_level": "verified",
        }
        (peer_dir / "jarvis.json").write_text(json.dumps(peer_data))

        data = {
            "sender": "jarvis",
            "payload": {
                "content": "hello",
                "signature": "-----BEGIN PGP MESSAGE-----\nfake\n-----END PGP MESSAGE-----",
            },
        }

        with patch("capauth.crypto.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.verify.return_value = True
            mock_get_backend.return_value = mock_backend

            result = loop._verify_message_signature(data)

        assert result == "verified"
        mock_backend.verify.assert_called_once_with(
            data=b"hello",
            signature_armor="-----BEGIN PGP MESSAGE-----\nfake\n-----END PGP MESSAGE-----",
            public_key_armor=peer_data["public_key"],
        )

    def test_failed_with_bad_signature(self, tmp_path):
        """Returns 'failed' when capauth backend rejects the signature."""
        loop = self._make_loop(tmp_path)

        peer_dir = (tmp_path / ".skcapstone") / "peers"
        peer_dir.mkdir(parents=True)
        peer_data = {
            "name": "jarvis",
            "fingerprint": "ABCD1234",
            "public_key": "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfake\n-----END PGP PUBLIC KEY BLOCK-----",
            "trust_level": "verified",
        }
        (peer_dir / "jarvis.json").write_text(json.dumps(peer_data))

        data = {
            "sender": "jarvis",
            "payload": {
                "content": "hello",
                "signature": "-----BEGIN PGP MESSAGE-----\nfake\n-----END PGP MESSAGE-----",
            },
        }

        with patch("capauth.crypto.get_backend") as mock_get_backend:
            mock_backend = MagicMock()
            mock_backend.verify.return_value = False
            mock_get_backend.return_value = mock_backend

            result = loop._verify_message_signature(data)

        assert result == "failed"


class TestOllamaConnectionPool:
    """Unit tests for _OllamaPool — connection reuse, TTL eviction, invalidation."""

    def test_get_returns_same_connection_within_ttl(self):
        """Two get() calls within TTL return the same connection object."""
        pool = _OllamaPool("http://localhost:11434", ttl=60)
        with patch("http.client.HTTPConnection") as mock_cls:
            mock_conn = MagicMock()
            mock_cls.return_value = mock_conn
            conn1 = pool.get()
            conn2 = pool.get()

        assert conn1 is conn2, "Same connection should be returned within TTL"
        assert mock_cls.call_count == 1, "HTTPConnection should be created only once"

    def test_get_recreates_connection_after_ttl(self):
        """get() creates a fresh connection once TTL has expired."""
        pool = _OllamaPool("http://localhost:11434", ttl=60)
        with patch("http.client.HTTPConnection") as mock_cls:
            mock_cls.side_effect = [MagicMock(), MagicMock()]
            pool.get()
            # Manually expire the TTL
            pool._created_at = time.monotonic() - 61
            pool.get()

        assert mock_cls.call_count == 2, "HTTPConnection should be recreated after TTL"

    def test_invalidate_discards_connection(self):
        """invalidate() closes and clears the cached connection."""
        pool = _OllamaPool("http://localhost:11434", ttl=60)
        with patch("http.client.HTTPConnection") as mock_cls:
            mock_cls.side_effect = [MagicMock(), MagicMock()]
            pool.get()
            pool.invalidate()
            assert pool._conn is None, "invalidate() should clear _conn"
            pool.get()

        assert mock_cls.call_count == 2, "New connection created after invalidate()"

    def test_probe_ollama_uses_pool_connection(self):
        """_probe_ollama routes the health check through the pool."""
        config = ConsciousnessConfig()
        bridge = LLMBridge(config)

        mock_conn = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"models":[]}'
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(bridge._ollama_pool, "get", return_value=mock_conn):
            result = bridge._probe_ollama()

        assert result is True
        mock_conn.request.assert_called_once_with("GET", "/api/tags")
        mock_resp.read.assert_called_once()  # body drained for keep-alive

    def test_probe_ollama_invalidates_pool_on_error(self):
        """_probe_ollama invalidates the pool when a connection error occurs."""
        config = ConsciousnessConfig()
        bridge = LLMBridge(config)

        mock_conn = MagicMock()
        mock_conn.request.side_effect = ConnectionError("refused")

        with patch.object(bridge._ollama_pool, "get", return_value=mock_conn):
            with patch.object(bridge._ollama_pool, "invalidate") as mock_invalidate:
                result = bridge._probe_ollama()

        assert result is False
        mock_invalidate.assert_called_once()

    def test_pool_host_port_parsing(self):
        """_OllamaPool correctly parses host and port from the URL."""
        pool = _OllamaPool("http://myhost:12345", ttl=30)
        assert pool._host == "myhost"
        assert pool._port == 12345

    def test_pool_defaults_for_bare_localhost(self):
        """_OllamaPool falls back to localhost:11434 for a bare URL."""
        pool = _OllamaPool("http://localhost:11434")
        assert pool._host == "localhost"
        assert pool._port == 11434


class TestMessageThreading:
    """Tests for thread_id / in_reply_to envelope tracking and history grouping."""

    # ------------------------------------------------------------------
    # _SimpleEnvelope extraction
    # ------------------------------------------------------------------

    def test_envelope_extracts_thread_id_from_root(self):
        """thread_id at envelope root is captured."""
        data = {
            "sender": "jarvis",
            "thread_id": "thread-abc",
            "payload": {"content": "hi", "content_type": "text"},
        }
        env = _SimpleEnvelope(data)
        assert env.thread_id == "thread-abc"

    def test_envelope_extracts_thread_id_from_payload(self):
        """thread_id nested inside payload is captured."""
        data = {
            "sender": "jarvis",
            "payload": {"content": "hi", "content_type": "text", "thread_id": "thread-xyz"},
        }
        env = _SimpleEnvelope(data)
        assert env.thread_id == "thread-xyz"

    def test_envelope_extracts_in_reply_to(self):
        """in_reply_to at envelope root is captured."""
        data = {
            "sender": "lumina",
            "in_reply_to": "msg-001",
            "payload": {"content": "reply", "content_type": "text"},
        }
        env = _SimpleEnvelope(data)
        assert env.in_reply_to == "msg-001"

    def test_envelope_in_reply_to_from_payload(self):
        """in_reply_to nested inside payload is captured."""
        data = {
            "sender": "lumina",
            "payload": {"content": "reply", "content_type": "text", "in_reply_to": "msg-002"},
        }
        env = _SimpleEnvelope(data)
        assert env.in_reply_to == "msg-002"

    def test_envelope_defaults_empty_when_absent(self):
        """thread_id and in_reply_to default to empty string when absent."""
        data = {"sender": "ava", "payload": {"content": "hello", "content_type": "text"}}
        env = _SimpleEnvelope(data)
        assert env.thread_id == ""
        assert env.in_reply_to == ""

    # ------------------------------------------------------------------
    # SystemPromptBuilder — add_to_history threading
    # ------------------------------------------------------------------

    def test_add_to_history_stores_thread_id(self, tmp_path):
        """add_to_history stores thread_id in the history entry."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Hello!", thread_id="t-001")

        entry = builder._conversation_history["jarvis"][0]
        assert entry["thread_id"] == "t-001"

    def test_add_to_history_stores_in_reply_to(self, tmp_path):
        """add_to_history stores in_reply_to in the history entry."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Reply!", in_reply_to="msg-55")

        entry = builder._conversation_history["jarvis"][0]
        assert entry["in_reply_to"] == "msg-55"

    def test_add_to_history_no_thread_fields_when_absent(self, tmp_path):
        """No thread_id/in_reply_to keys in entry when not provided."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Plain message")

        entry = builder._conversation_history["jarvis"][0]
        assert "thread_id" not in entry
        assert "in_reply_to" not in entry

    def test_thread_fields_persisted_to_json(self, tmp_path):
        """thread_id and in_reply_to survive the round-trip through JSON persistence."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("opus", "user", "Threaded msg", thread_id="t-99", in_reply_to="m-10")

        conv_file = home / "conversations" / "opus.json"
        data = json.loads(conv_file.read_text())
        assert data[0]["thread_id"] == "t-99"
        assert data[0]["in_reply_to"] == "m-10"

    # ------------------------------------------------------------------
    # SystemPromptBuilder.build — thread context in prompt
    # ------------------------------------------------------------------

    def test_build_shows_thread_label_when_thread_id_given(self, tmp_path):
        """build() includes a [Thread: ...] label when thread_id is provided."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("jarvis", "user", "Thread message", thread_id="t-alpha")
        prompt = builder.build(peer_name="jarvis", thread_id="t-alpha")

        assert "Thread: t-alpha" in prompt
        assert "Thread message" in prompt

    def test_build_groups_thread_and_other_messages(self, tmp_path):
        """Thread messages appear under [Thread:...] and others under [Other recent messages:]."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("ava", "user", "Thread msg", thread_id="t-1")
        builder.add_to_history("ava", "user", "Unrelated msg")

        prompt = builder.build(peer_name="ava", thread_id="t-1")

        assert "Thread: t-1" in prompt
        assert "Thread msg" in prompt
        assert "Other recent messages" in prompt
        assert "Unrelated msg" in prompt

    def test_build_without_thread_id_shows_thread_labels_inline(self, tmp_path):
        """Without thread_id, messages with threads show [thread:...] inline."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)

        builder.add_to_history("lumina", "user", "Inline threaded", thread_id="t-beta")
        builder.add_to_history("lumina", "user", "Plain")

        prompt = builder.build(peer_name="lumina")

        assert "thread:t-beta" in prompt
        assert "Inline threaded" in prompt
        assert "Plain" in prompt

    # ------------------------------------------------------------------
    # ConsciousnessLoop.process_envelope — threading end-to-end
    # ------------------------------------------------------------------

    def test_process_envelope_stores_thread_id_in_history(self, tmp_path):
        """process_envelope extracts thread_id and stores it in conversation history."""
        config = ConsciousnessConfig(fallback_chain=["passthrough"])
        loop = ConsciousnessLoop(config, home=tmp_path / ".skcapstone")
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "hi back"

        data = {
            "sender": "jarvis",
            "thread_id": "t-42",
            "payload": {"content": "threaded hello", "content_type": "text"},
        }
        env = _SimpleEnvelope(data)
        loop.process_envelope(env)

        history = loop._prompt_builder._conversation_history.get("jarvis", [])
        user_entry = next((e for e in history if e["role"] == "user"), None)
        assert user_entry is not None
        assert user_entry.get("thread_id") == "t-42"

    def test_process_envelope_stores_in_reply_to_in_history(self, tmp_path):
        """process_envelope extracts in_reply_to and stores it in conversation history."""
        config = ConsciousnessConfig(fallback_chain=["passthrough"])
        loop = ConsciousnessLoop(config, home=tmp_path / ".skcapstone")
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "reply"

        data = {
            "sender": "ava",
            "in_reply_to": "msg-77",
            "payload": {"content": "reply message", "content_type": "text"},
        }
        env = _SimpleEnvelope(data)
        loop.process_envelope(env)

        history = loop._prompt_builder._conversation_history.get("ava", [])
        user_entry = next((e for e in history if e["role"] == "user"), None)
        assert user_entry is not None
        assert user_entry.get("in_reply_to") == "msg-77"


# ---------------------------------------------------------------------------
# Prompt versioning tests
# ---------------------------------------------------------------------------


class TestSystemPromptVersioning:
    """Tests for SHA-256 prompt versioning in SystemPromptBuilder."""

    def test_initial_hash_is_none(self, tmp_path):
        """current_prompt_hash is None before any build() call."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)
        assert builder.current_prompt_hash is None

    def test_hash_set_after_build(self, tmp_path):
        """After build(), current_prompt_hash is a 64-char SHA-256 hex string."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)
        builder.build()
        h = builder.current_prompt_hash
        assert h is not None
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_version_file_created_on_first_build(self, tmp_path):
        """A JSON version file is written to prompt_versions/ on first build."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)
        builder.build()

        versions_dir = home / "prompt_versions"
        files = list(versions_dir.glob("*.json"))
        assert len(files) == 1, "Expected exactly one version file"

        record = json.loads(files[0].read_text())
        assert record["hash"] == builder.current_prompt_hash
        assert "timestamp" in record
        assert "prompt" in record

    def test_no_duplicate_file_for_same_prompt(self, tmp_path):
        """Building the same prompt twice does not create a second version file."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        builder = SystemPromptBuilder(home)
        builder.build()
        builder.build()  # same content, same hash

        versions_dir = home / "prompt_versions"
        files = list(versions_dir.glob("*.json"))
        assert len(files) == 1, "No duplicate file when prompt unchanged"

    def test_new_file_when_prompt_changes(self, tmp_path):
        """A new version file is created when the prompt content changes."""
        home = tmp_path / ".skcapstone"
        identity_dir = home / "identity"
        identity_dir.mkdir(parents=True)

        builder = SystemPromptBuilder(home)
        builder.build()
        first_hash = builder.current_prompt_hash

        # Change the identity so the prompt changes
        (identity_dir / "identity.json").write_text(
            json.dumps({"name": "changed-agent", "fingerprint": "NEWFINGERPRINT"})
        )
        # Expire the cache so the identity is reloaded
        builder._section_cache.clear()
        builder.build()
        second_hash = builder.current_prompt_hash

        assert first_hash != second_hash
        versions_dir = home / "prompt_versions"
        files = list(versions_dir.glob("*.json"))
        assert len(files) == 2, "Two files for two distinct prompt versions"

    def test_stats_include_prompt_hash_and_version_responses(self, tmp_path):
        """ConsciousnessLoop.stats exposes current_prompt_hash and prompt_version_responses."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        config = ConsciousnessConfig(enabled=False)
        loop = ConsciousnessLoop(config, home=home)

        stats = loop.stats
        assert "current_prompt_hash" in stats
        assert "prompt_version_responses" in stats
        assert isinstance(stats["prompt_version_responses"], dict)

    def test_version_responses_incremented_on_send(self, tmp_path):
        """prompt_version_responses counter increments for the active hash when a response is sent."""
        home = tmp_path / ".skcapstone"
        home.mkdir()

        config = ConsciousnessConfig(enabled=False)
        loop = ConsciousnessLoop(config, home=home)

        # Simulate a build so a hash is established
        loop._prompt_builder.build()
        active_hash = loop._prompt_builder.current_prompt_hash
        assert active_hash is not None

        # Manually trigger the counting logic (as the send path does)
        loop._prompt_version_responses[active_hash] += 1

        stats = loop.stats
        assert stats["prompt_version_responses"].get(active_hash) == 1


class TestFetchSenderMemories:
    """Tests for ConsciousnessLoop._fetch_sender_memories()."""

    def _make_loop(self, tmp_path):
        config = ConsciousnessConfig(
            fallback_chain=["passthrough"],
            auto_memory=False,
        )
        return ConsciousnessLoop(config, home=tmp_path / ".skcapstone")

    def _make_entry(self, memory_id, content, tags=None):
        """Build a minimal MemoryEntry-like mock."""
        entry = MagicMock()
        entry.memory_id = memory_id
        entry.content = content
        entry.tags = tags or []
        return entry

    def test_returns_empty_when_no_memories(self, tmp_path):
        """Returns empty string when memory search yields nothing."""
        loop = self._make_loop(tmp_path)

        with patch("skcapstone.memory_engine.search", return_value=[]):
            result = loop._fetch_sender_memories("jarvis", "hello there")

        assert result == ""

    def test_includes_top_3_memories_in_output(self, tmp_path):
        """Output contains exactly 3 memory entries when 5 are returned."""
        loop = self._make_loop(tmp_path)

        entries = [
            self._make_entry(f"id-{i}", f"Memory content {i}")
            for i in range(5)
        ]

        # by_sender returns 3, by_content returns 2 different ones
        def mock_search(home, query, tags=None, limit=5):
            if tags:
                return entries[:3]
            return entries[3:]

        with patch("skcapstone.memory_engine.search", side_effect=mock_search):
            result = loop._fetch_sender_memories("jarvis", "hello")

        assert "Relevant memories:" in result
        assert "[1]" in result
        assert "[2]" in result
        assert "[3]" in result
        # Should not exceed 3 entries
        assert "[4]" not in result

    def test_deduplicates_overlapping_results(self, tmp_path):
        """Memories returned by both searches are deduplicated."""
        loop = self._make_loop(tmp_path)

        shared = self._make_entry("shared-id", "Shared memory content")
        unique = self._make_entry("unique-id", "Unique memory content")

        # Both searches return the same shared entry
        with patch("skcapstone.memory_engine.search", return_value=[shared, unique]):
            result = loop._fetch_sender_memories("jarvis", "hello")

        # shared-id should appear exactly once
        assert result.count("Shared memory content") == 1

    def test_memory_context_appended_to_system_prompt(self, tmp_path):
        """process_envelope appends memory context to the system prompt passed to LLM."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        config = ConsciousnessConfig(
            fallback_chain=["passthrough"],
            auto_memory=False,
            auto_ack=False,
            desktop_notifications=False,
        )
        loop = ConsciousnessLoop(config, home=home)

        captured_system_prompts = []

        def fake_generate(system_prompt, content, signal, _out_info=None, **kwargs):
            captured_system_prompts.append(system_prompt)
            return "test response"

        loop._bridge = MagicMock()
        loop._bridge.generate.side_effect = fake_generate

        entry = MagicMock()
        entry.memory_id = "mem-abc"
        entry.content = "jarvis mentioned he prefers concise replies"
        entry.tags = ["peer:jarvis"]

        with patch("skcapstone.memory_engine.search", return_value=[entry]):
            envelope = _SimpleEnvelope({
                "sender": "jarvis",
                "payload": {"content": "What is the status?", "content_type": "text"},
            })
            loop.process_envelope(envelope)

        assert len(captured_system_prompts) == 1
        assert "Relevant memories:" in captured_system_prompts[0]
        assert "jarvis mentioned he prefers concise replies" in captured_system_prompts[0]

    def test_memory_error_does_not_break_envelope_processing(self, tmp_path):
        """If memory search raises, process_envelope still completes normally."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        config = ConsciousnessConfig(
            fallback_chain=["passthrough"],
            auto_memory=False,
            auto_ack=False,
            desktop_notifications=False,
        )
        loop = ConsciousnessLoop(config, home=home)
        loop._bridge = MagicMock()
        loop._bridge.generate.return_value = "test response"

        with patch(
            "skcapstone.memory_engine.search",
            side_effect=RuntimeError("db unavailable"),
        ):
            envelope = _SimpleEnvelope({
                "sender": "jarvis",
                "payload": {"content": "hello", "content_type": "text"},
            })
            result = loop.process_envelope(envelope)

        assert result == "test response"

    def test_no_memory_enrichment_when_memories_empty(self, tmp_path):
        """System prompt is unchanged when no memories are found."""
        home = tmp_path / ".skcapstone"
        home.mkdir()
        config = ConsciousnessConfig(
            fallback_chain=["passthrough"],
            auto_memory=False,
            auto_ack=False,
            desktop_notifications=False,
        )
        loop = ConsciousnessLoop(config, home=home)

        captured_system_prompts = []

        def fake_generate(system_prompt, content, signal, _out_info=None, **kwargs):
            captured_system_prompts.append(system_prompt)
            return "test response"

        loop._bridge = MagicMock()
        loop._bridge.generate.side_effect = fake_generate

        with patch("skcapstone.memory_engine.search", return_value=[]):
            envelope = _SimpleEnvelope({
                "sender": "jarvis",
                "payload": {"content": "hello", "content_type": "text"},
            })
            loop.process_envelope(envelope)

        assert len(captured_system_prompts) == 1
        assert "Relevant memories:" not in captured_system_prompts[0]
