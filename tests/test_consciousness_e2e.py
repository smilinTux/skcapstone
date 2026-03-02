"""E2E tests for the consciousness pipeline — no real LLM needed."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.consciousness_loop import (
    ConsciousnessConfig,
    ConsciousnessLoop,
    LLMBridge,
    SystemPromptBuilder,
    InboxHandler,
    _SimpleEnvelope,
    _classify_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_loop(tmp_path: Path, config: ConsciousnessConfig | None = None) -> ConsciousnessLoop:
    """Create a ConsciousnessLoop with tmp dirs and no real backend probing."""
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    shared_root = tmp_path / "shared"
    shared_root.mkdir(parents=True, exist_ok=True)

    if config is None:
        config = ConsciousnessConfig(
            auto_memory=False,   # avoid skcapstone.memory_engine I/O
            auto_ack=False,      # no SKComm needed
            use_inotify=False,   # no filesystem watcher needed
        )

    with patch.object(LLMBridge, "_probe_ollama", return_value=False):
        loop = ConsciousnessLoop(config, home=home, shared_root=shared_root)

    return loop


def _text_envelope(content: str, sender: str = "test-peer") -> _SimpleEnvelope:
    """Build a minimal text envelope."""
    return _SimpleEnvelope({
        "sender": sender,
        "payload": {"content": content, "content_type": "text"},
    })


def _ack_envelope() -> _SimpleEnvelope:
    """Build a minimal ACK envelope."""
    return _SimpleEnvelope({
        "sender": "test-peer",
        "payload": {"content": "ACK", "content_type": "ack"},
    })


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFullPipelineMockLLM:
    """test_full_pipeline_mock_llm — ConsciousnessLoop with a mock LLMBridge."""

    def test_returns_response(self, tmp_path):
        """process_envelope() returns the LLM response."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = "hello back"
        mock_bridge.available_backends = {"passthrough": True}
        loop._bridge = mock_bridge

        envelope = _text_envelope("hello world")
        result = loop.process_envelope(envelope)

        assert result == "hello back"

    def test_messages_processed_increments(self, tmp_path):
        """stats['messages_processed'] is 1 after processing one message."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = "response"
        mock_bridge.available_backends = {"passthrough": True}
        loop._bridge = mock_bridge

        assert loop.stats["messages_processed"] == 0
        loop.process_envelope(_text_envelope("ping"))
        assert loop.stats["messages_processed"] == 1


class TestPipelineSkipsAckMessages:
    """test_pipeline_skips_ack_messages — ACK envelopes are ignored."""

    def test_ack_returns_none(self, tmp_path):
        """process_envelope() returns None for an ACK message."""
        loop = _make_loop(tmp_path)
        result = loop.process_envelope(_ack_envelope())
        assert result is None

    def test_ack_does_not_increment_counter(self, tmp_path):
        """ACK messages do NOT increment messages_processed."""
        loop = _make_loop(tmp_path)
        loop.process_envelope(_ack_envelope())
        # ACK is skipped before the counter increment
        assert loop.stats["messages_processed"] == 0

    @pytest.mark.parametrize("skip_type", ["heartbeat", "file", "file_chunk", "file_manifest"])
    def test_other_skip_types_return_none(self, tmp_path, skip_type):
        """process_envelope() returns None for all non-text content types."""
        loop = _make_loop(tmp_path)
        envelope = _SimpleEnvelope({
            "sender": "peer",
            "payload": {"content": "data", "content_type": skip_type},
        })
        assert loop.process_envelope(envelope) is None


class TestPipelineStoresConversationHistory:
    """test_pipeline_stores_conversation_history — history is updated."""

    def test_user_message_in_history(self, tmp_path):
        """After processing, the sender's message appears in conversation history."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = "got it"
        mock_bridge.available_backends = {"passthrough": True}
        loop._bridge = mock_bridge

        loop.process_envelope(_text_envelope("what is the answer?", sender="alice"))

        history = loop._prompt_builder._conversation_history
        assert "alice" in history
        user_msgs = [m for m in history["alice"] if m.get("role") == "user"]
        assert any("what is the answer?" in m.get("content", "") for m in user_msgs)

    def test_assistant_reply_in_history(self, tmp_path):
        """After processing, the assistant reply is stored in history."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.return_value = "forty-two"
        mock_bridge.available_backends = {"passthrough": True}
        loop._bridge = mock_bridge

        loop.process_envelope(_text_envelope("what is the answer?", sender="bob"))

        history = loop._prompt_builder._conversation_history
        assistant_msgs = [m for m in history.get("bob", []) if m.get("role") == "assistant"]
        assert any("forty-two" in m.get("content", "") for m in assistant_msgs)


class TestPipelineHandlesLLMFailure:
    """test_pipeline_handles_llm_failure — errors are counted and None returned."""

    def test_returns_none_on_generate_exception(self, tmp_path):
        """process_envelope() returns None when LLMBridge.generate() raises."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.side_effect = RuntimeError("backend unavailable")
        mock_bridge.available_backends = {}
        loop._bridge = mock_bridge

        result = loop.process_envelope(_text_envelope("hello"))
        assert result is None

    def test_errors_count_increments(self, tmp_path):
        """errors counter increments when LLMBridge.generate() raises."""
        loop = _make_loop(tmp_path)

        mock_bridge = MagicMock()
        mock_bridge.generate.side_effect = RuntimeError("boom")
        mock_bridge.available_backends = {}
        loop._bridge = mock_bridge

        assert loop.stats["errors"] == 0
        loop.process_envelope(_text_envelope("hello"))
        assert loop.stats["errors"] == 1


class _FakeEvent:
    """Minimal watchdog-style file creation event."""

    def __init__(self, src_path: str, is_directory: bool = False) -> None:
        self.src_path = src_path
        self.is_directory = is_directory
        self.event_type = "created"


class TestInotifyHandlerDebounce:
    """test_inotify_handler_debounce — rapid duplicate events are collapsed."""

    def test_single_call_on_rapid_duplicates(self):
        """Callback is invoked only once when the same path fires twice within debounce window."""
        calls: list[Path] = []
        handler = InboxHandler(callback=calls.append, debounce_ms=5000)

        event = _FakeEvent("/inbox/msg.skc.json")
        handler.on_created(event)
        handler.on_created(event)  # rapid second fire — within 5 s

        assert len(calls) == 1

    def test_two_calls_after_debounce_expires(self):
        """Callback is invoked twice when events are separated by > debounce window."""
        calls: list[Path] = []
        handler = InboxHandler(callback=calls.append, debounce_ms=50)  # 50 ms

        event = _FakeEvent("/inbox/msg2.skc.json")
        handler.on_created(event)
        time.sleep(0.06)  # 60 ms — past the 50 ms window
        handler.on_created(event)

        assert len(calls) == 2

    def test_ignores_non_skc_json_files(self):
        """Callback is NOT called for non-.skc.json files."""
        calls: list[Path] = []
        handler = InboxHandler(callback=calls.append)

        handler.on_created(_FakeEvent("/inbox/somefile.txt"))
        handler.on_created(_FakeEvent("/inbox/data.json"))

        assert len(calls) == 0

    def test_ignores_directory_events(self):
        """Callback is NOT called for directory creation events."""
        calls: list[Path] = []
        handler = InboxHandler(callback=calls.append)

        handler.on_created(_FakeEvent("/inbox/subdir", is_directory=True))

        assert len(calls) == 0


class TestClassifyMessageTags:
    """test_classify_message_tags — keyword-to-tag mapping."""

    @pytest.mark.parametrize("msg,expected_tag", [
        ("please debug this function", "code"),
        ("can you fix this error?", "code"),
        ("implement the class now", "code"),
        ("can you analyze the architecture", "analyze"),
        ("explain why this fails", "analyze"),
        ("write a story about a penguin", "creative"),
        ("compose a marketing email", "creative"),
        ("hi", "simple"),
        ("hello there", "simple"),
        ("ok", "simple"),
        ("the clouds look nice today", "general"),
    ])
    def test_tag_assigned(self, msg: str, expected_tag: str):
        """Correct tag is assigned based on keyword presence."""
        signal = _classify_message(msg)
        assert expected_tag in signal.tags, (
            f"Expected tag {expected_tag!r} for message {msg!r}, got {signal.tags}"
        )

    def test_general_tag_when_no_keywords(self):
        """Messages with no matching keywords get exactly ['general']."""
        signal = _classify_message("the sky is beautiful today")
        assert signal.tags == ["general"]

    def test_multiple_tags_on_overlap(self):
        """Messages matching multiple keyword sets receive multiple tags."""
        signal = _classify_message("debug and analyze this code architecture")
        assert "code" in signal.tags
        assert "analyze" in signal.tags

    def test_simple_tag_requires_short_message(self):
        """'simple' tag is only assigned if the message is < 50 chars."""
        short_msg = "hello"
        long_msg = "hello " + "x" * 50  # > 50 chars
        assert "simple" in _classify_message(short_msg).tags
        assert "simple" not in _classify_message(long_msg).tags

    def test_token_estimate(self):
        """Estimated tokens is approximately content_length / 4."""
        msg = "a" * 400
        signal = _classify_message(msg)
        assert signal.estimated_tokens == 100

    def test_description_capped_at_100_chars(self):
        """TaskSignal description is capped at 100 characters."""
        msg = "x" * 200
        signal = _classify_message(msg)
        assert len(signal.description) == 100
