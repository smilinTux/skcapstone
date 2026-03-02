"""Tests for ConversationStore and chat history CLI.

Covers:
  - ConversationStore.append / get_last / load / all_peers / clear
  - Path-traversal sanitization
  - ConsciousnessLoop integrates ConversationStore (last-10 context)
  - `skcapstone chat history PEER` CLI command
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from skcapstone.conversation_store import ConversationStore, _sanitize_peer_name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    """ConversationStore rooted in a temp directory."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    return ConversationStore(home)


@pytest.fixture
def populated_store(store, tmp_path):
    """Store with two peers pre-seeded."""
    store.append("alice", "user", "hello alice")
    store.append("alice", "assistant", "hi there!")
    store.append("bob", "user", "hey bob")
    return store


@pytest.fixture
def agent_home(tmp_path):
    """Minimal agent home for CLI tests."""
    home = tmp_path / ".skcapstone"
    (home / "identity").mkdir(parents=True)
    (home / "config").mkdir(parents=True)
    identity = {"name": "TestAgent", "fingerprint": "AABB1234", "capauth_managed": False}
    (home / "identity" / "identity.json").write_text(json.dumps(identity))
    (home / "manifest.json").write_text(json.dumps({"name": "TestAgent", "version": "0.1.0"}))
    import yaml
    (home / "config" / "config.yaml").write_text(yaml.dump({"agent_name": "TestAgent"}))
    return home


# ---------------------------------------------------------------------------
# _sanitize_peer_name
# ---------------------------------------------------------------------------


class TestSanitizePeerName:
    def test_normal_name_unchanged(self):
        assert _sanitize_peer_name("lumina") == "lumina"

    def test_strips_path_separators(self):
        result = _sanitize_peer_name("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_strips_null_bytes(self):
        assert "\x00" not in _sanitize_peer_name("evil\x00peer")

    def test_empty_returns_unknown(self):
        assert _sanitize_peer_name("") == "unknown"

    def test_none_returns_unknown(self):
        assert _sanitize_peer_name(None) == "unknown"  # type: ignore[arg-type]

    def test_max_length_64(self):
        assert len(_sanitize_peer_name("a" * 100)) == 64

    def test_allowed_chars_preserved(self):
        assert _sanitize_peer_name("user@domain.io") == "user@domain.io"
        assert _sanitize_peer_name("my-peer_01") == "my-peer_01"


# ---------------------------------------------------------------------------
# ConversationStore — basic operations
# ---------------------------------------------------------------------------


class TestConversationStoreAppend:
    def test_append_creates_file(self, store, tmp_path):
        store.append("alice", "user", "hello")
        assert (tmp_path / ".skcapstone" / "conversations" / "alice.json").exists()

    def test_append_returns_message_dict(self, store):
        msg = store.append("alice", "user", "hello")
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert "timestamp" in msg

    def test_append_multiple_messages(self, store):
        store.append("alice", "user", "msg1")
        store.append("alice", "assistant", "msg2")
        history = store.load("alice")
        assert len(history) == 2
        assert history[0]["content"] == "msg1"
        assert history[1]["content"] == "msg2"

    def test_append_stores_thread_id(self, store):
        store.append("alice", "user", "threaded", thread_id="t-01")
        history = store.load("alice")
        assert history[0].get("thread_id") == "t-01"

    def test_append_stores_in_reply_to(self, store):
        store.append("alice", "assistant", "reply", in_reply_to="msg-123")
        history = store.load("alice")
        assert history[0].get("in_reply_to") == "msg-123"

    def test_append_path_traversal_sanitized(self, store, tmp_path):
        """Malicious peer name is sanitized; no file written outside conversations/."""
        store.append("../../evil", "user", "attack")
        conv_dir = tmp_path / ".skcapstone" / "conversations"
        files = list(conv_dir.glob("*.json"))
        # The sanitized name must not contain path separators
        for f in files:
            assert "/" not in f.name
            assert ".." not in f.name


class TestConversationStoreGetLast:
    def test_returns_last_n(self, store):
        for i in range(15):
            store.append("alice", "user", f"msg{i}")
        last5 = store.get_last("alice", 5)
        assert len(last5) == 5
        assert last5[-1]["content"] == "msg14"

    def test_returns_all_when_n_larger(self, store):
        store.append("alice", "user", "only one")
        result = store.get_last("alice", 10)
        assert len(result) == 1

    def test_empty_for_unknown_peer(self, store):
        assert store.get_last("nobody", 10) == []

    def test_n_zero_returns_empty(self, store):
        store.append("alice", "user", "hi")
        assert store.get_last("alice", 0) == []

    def test_default_n_is_10(self, store):
        for i in range(20):
            store.append("alice", "user", f"msg{i}")
        assert len(store.get_last("alice")) == 10


class TestConversationStoreAllPeers:
    def test_empty_when_no_dir(self, tmp_path):
        store = ConversationStore(tmp_path / "empty")
        assert store.all_peers() == []

    def test_lists_all_peers(self, populated_store):
        peers = populated_store.all_peers()
        assert "alice" in peers
        assert "bob" in peers

    def test_sorted_alphabetically(self, store):
        store.append("zara", "user", "hi")
        store.append("anna", "user", "hi")
        peers = store.all_peers()
        assert peers == sorted(peers)


class TestConversationStoreClear:
    def test_clear_removes_file(self, populated_store, tmp_path):
        populated_store.clear("bob")
        assert not (tmp_path / ".skcapstone" / "conversations" / "bob.json").exists()

    def test_clear_returns_true_when_existed(self, populated_store):
        assert populated_store.clear("alice") is True

    def test_clear_returns_false_when_missing(self, store):
        assert store.clear("nobody") is False

    def test_clear_does_not_affect_other_peers(self, populated_store):
        populated_store.clear("bob")
        assert populated_store.load("alice") != []


class TestConversationStoreFormatForPrompt:
    def test_returns_empty_for_unknown_peer(self, store):
        assert store.format_for_prompt("nobody") == ""

    def test_includes_peer_name_header(self, store):
        store.append("alice", "user", "hi")
        result = store.format_for_prompt("alice")
        assert "alice" in result

    def test_includes_role_and_content(self, store):
        store.append("alice", "user", "how are you?")
        store.append("alice", "assistant", "doing great!")
        result = store.format_for_prompt("alice")
        assert "[user]" in result
        assert "[assistant]" in result
        assert "how are you?" in result


# ---------------------------------------------------------------------------
# ConsciousnessLoop integration — uses ConversationStore for context
# ---------------------------------------------------------------------------


class TestConsciousnessLoopUsesConversationStore:
    """Verify ConsciousnessLoop wires ConversationStore into SystemPromptBuilder."""

    def test_loop_creates_conv_store(self, tmp_path):
        from skcapstone.consciousness_loop import ConsciousnessConfig, ConsciousnessLoop
        from skcapstone.conversation_store import ConversationStore

        home = tmp_path / ".skcapstone"
        home.mkdir()
        config = ConsciousnessConfig()
        loop = ConsciousnessLoop(config, home=home, shared_root=home)
        assert isinstance(loop._conv_store, ConversationStore)

    def test_prompt_builder_has_conv_store(self, tmp_path):
        from skcapstone.consciousness_loop import ConsciousnessConfig, ConsciousnessLoop

        home = tmp_path / ".skcapstone"
        home.mkdir()
        config = ConsciousnessConfig()
        loop = ConsciousnessLoop(config, home=home, shared_root=home)
        assert loop._prompt_builder._conv_store is loop._conv_store

    def test_add_to_history_writes_via_conv_store(self, tmp_path):
        """add_to_history via ConversationStore creates the JSON file."""
        from skcapstone.consciousness_loop import SystemPromptBuilder
        from skcapstone.conversation_store import ConversationStore

        home = tmp_path / ".skcapstone"
        home.mkdir()
        store = ConversationStore(home)
        builder = SystemPromptBuilder(home=home, conv_store=store)

        builder.add_to_history("testpeer", "user", "hello world")

        history = store.load("testpeer")
        assert len(history) == 1
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "hello world"

    def test_get_peer_history_reads_from_store(self, tmp_path):
        """_get_peer_history returns content written directly to the store."""
        from skcapstone.consciousness_loop import SystemPromptBuilder
        from skcapstone.conversation_store import ConversationStore

        home = tmp_path / ".skcapstone"
        home.mkdir()
        store = ConversationStore(home)
        # Write directly to the store (bypassing prompt builder)
        store.append("opus", "user", "direct write")
        store.append("opus", "assistant", "got it")

        # Build prompt builder with the same store
        builder = SystemPromptBuilder(home=home, conv_store=store)
        history_text = builder._get_peer_history("opus")

        assert "opus" in history_text
        assert "direct write" in history_text
        assert "got it" in history_text

    def test_loads_last_10_messages_for_context(self, tmp_path):
        """Context includes at most max_history_messages (10) entries."""
        from skcapstone.consciousness_loop import SystemPromptBuilder
        from skcapstone.conversation_store import ConversationStore

        home = tmp_path / ".skcapstone"
        home.mkdir()
        store = ConversationStore(home)
        for i in range(20):
            store.append("lumina", "user", f"msg{i}")

        builder = SystemPromptBuilder(home=home, conv_store=store, max_history_messages=10)
        history_text = builder._get_peer_history("lumina")

        # Only messages 10–19 should appear
        assert "msg19" in history_text
        assert "msg0" not in history_text


# ---------------------------------------------------------------------------
# `skcapstone chat history PEER` CLI
# ---------------------------------------------------------------------------


class TestChatHistoryCLI:
    """Tests for `skcapstone chat history PEER`."""

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_help(self, _mock_rt):
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["chat", "history", "--help"])
        assert result.exit_code == 0
        assert "PEER" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_empty(self, _mock_rt, agent_home):
        """No conversation → 'No conversation history' message."""
        from skcapstone.cli import main
        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "history", "nobody", "--home", str(agent_home)]
        )
        assert result.exit_code == 0
        assert "No conversation history" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_shows_messages(self, _mock_rt, agent_home):
        """Messages written to store appear in history output."""
        from skcapstone.cli import main
        from skcapstone.conversation_store import ConversationStore

        store = ConversationStore(agent_home)
        store.append("lumina", "user", "Hello Lumina!")
        store.append("lumina", "assistant", "Hello! How can I help?")

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "history", "lumina", "--home", str(agent_home)]
        )
        assert result.exit_code == 0
        assert "Hello Lumina!" in result.output
        assert "Hello! How can I help?" in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_limit(self, _mock_rt, agent_home):
        """--limit N restricts output to last N messages."""
        from skcapstone.cli import main
        from skcapstone.conversation_store import ConversationStore

        store = ConversationStore(agent_home)
        for i in range(10):
            store.append("lumina", "user", f"msg{i}")

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "history", "lumina", "--limit", "3", "--home", str(agent_home)]
        )
        assert result.exit_code == 0
        assert "msg9" in result.output
        assert "msg0" not in result.output

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_json_output(self, _mock_rt, agent_home):
        """--json flag outputs valid JSON list."""
        from skcapstone.cli import main
        from skcapstone.conversation_store import ConversationStore

        store = ConversationStore(agent_home)
        store.append("opus", "user", "test message")

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "history", "opus", "--json", "--home", str(agent_home)]
        )
        assert result.exit_code == 0
        data = json.loads(result.output.strip())
        assert isinstance(data, list)
        assert data[0]["content"] == "test message"

    @patch("skcapstone.cli.chat.get_runtime")
    def test_history_role_labels(self, _mock_rt, agent_home):
        """Both user and assistant roles appear in the formatted output."""
        from skcapstone.cli import main
        from skcapstone.conversation_store import ConversationStore

        store = ConversationStore(agent_home)
        store.append("jarvis", "user", "status?")
        store.append("jarvis", "assistant", "all systems nominal")

        runner = CliRunner()
        result = runner.invoke(
            main, ["chat", "history", "jarvis", "--home", str(agent_home)]
        )
        assert result.exit_code == 0
        assert "user" in result.output
        assert "assistant" in result.output
