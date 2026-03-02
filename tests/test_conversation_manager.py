"""Tests for ConversationManager — centralized peer conversation management."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.conversation_manager import ConversationManager, _sanitize_peer_name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path) -> Path:
    """Minimal agent home with a conversations directory."""
    h = tmp_path / ".skcapstone"
    h.mkdir()
    return h


@pytest.fixture
def manager(home) -> ConversationManager:
    """Fresh ConversationManager on an empty home."""
    return ConversationManager(home, max_history_messages=10)


@pytest.fixture
def populated_home(tmp_path) -> Path:
    """Agent home pre-populated with two peers."""
    h = tmp_path / ".skcapstone"
    conv_dir = h / "conversations"
    conv_dir.mkdir(parents=True)
    alice = [
        {"role": "user", "content": "Hello Alice", "timestamp": "2026-01-01T10:00:00+00:00"},
        {"role": "assistant", "content": "Hi there!", "timestamp": "2026-01-01T10:00:01+00:00"},
    ]
    bob = [
        {"role": "user", "content": "Hey Bob", "timestamp": "2026-01-02T12:00:00+00:00"},
    ]
    (conv_dir / "alice.json").write_text(json.dumps(alice), encoding="utf-8")
    (conv_dir / "bob.json").write_text(json.dumps(bob), encoding="utf-8")
    return h


# ---------------------------------------------------------------------------
# _sanitize_peer_name unit tests
# ---------------------------------------------------------------------------


class TestSanitizePeerName:
    def test_normal_name(self):
        assert _sanitize_peer_name("alice") == "alice"

    def test_strips_slashes(self):
        result = _sanitize_peer_name("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_strips_null_bytes(self):
        assert "\x00" not in _sanitize_peer_name("evil\x00peer")

    def test_empty_returns_unknown(self):
        assert _sanitize_peer_name("") == "unknown"

    def test_none_returns_unknown(self):
        assert _sanitize_peer_name(None) == "unknown"  # type: ignore[arg-type]

    def test_max_length(self):
        assert len(_sanitize_peer_name("a" * 100)) == 64

    def test_allowed_chars(self):
        assert _sanitize_peer_name("user@domain.io") == "user@domain.io"
        assert _sanitize_peer_name("my-peer_01") == "my-peer_01"


# ---------------------------------------------------------------------------
# list_peers
# ---------------------------------------------------------------------------


class TestListPeers:
    def test_empty_returns_empty_list(self, manager):
        assert manager.list_peers() == []

    def test_shows_peer_after_message(self, manager):
        manager.add_message("jarvis", "user", "Hello")
        peers = manager.list_peers()
        assert len(peers) == 1
        assert peers[0]["peer"] == "jarvis"

    def test_includes_message_count(self, manager):
        manager.add_message("jarvis", "user", "msg1")
        manager.add_message("jarvis", "assistant", "msg2")
        peers = manager.list_peers()
        assert peers[0]["message_count"] == 2

    def test_includes_last_message_preview(self, manager):
        manager.add_message("jarvis", "user", "first")
        manager.add_message("jarvis", "assistant", "second")
        peers = manager.list_peers()
        assert "second" in peers[0]["last_message_preview"]

    def test_includes_last_message_time(self, manager):
        manager.add_message("jarvis", "user", "hello")
        peers = manager.list_peers()
        assert peers[0]["last_message_time"] is not None

    def test_loads_from_disk_on_init(self, populated_home):
        mgr = ConversationManager(populated_home)
        peers = {p["peer"] for p in mgr.list_peers()}
        assert "alice" in peers
        assert "bob" in peers

    def test_multiple_peers_all_listed(self, manager):
        manager.add_message("alice", "user", "hi")
        manager.add_message("bob", "user", "hey")
        peers = {p["peer"] for p in manager.list_peers()}
        assert peers == {"alice", "bob"}


# ---------------------------------------------------------------------------
# get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def test_unknown_peer_returns_empty(self, manager):
        assert manager.get_history("nobody") == []

    def test_returns_added_messages(self, manager):
        manager.add_message("jarvis", "user", "Hello")
        manager.add_message("jarvis", "assistant", "Hi")
        history = manager.get_history("jarvis")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"

    def test_returns_copy_not_reference(self, manager):
        manager.add_message("jarvis", "user", "msg")
        h1 = manager.get_history("jarvis")
        h1.append({"role": "fake"})
        h2 = manager.get_history("jarvis")
        assert len(h2) == 1  # original unaffected

    def test_loads_existing_from_disk(self, populated_home):
        mgr = ConversationManager(populated_home)
        history = mgr.get_history("alice")
        assert len(history) == 2
        assert history[0]["content"] == "Hello Alice"

    def test_sanitizes_peer_name(self, manager):
        manager.add_message("../evil", "user", "test")
        # sanitized name has no slashes
        result = manager.get_history("../evil")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# add_message
# ---------------------------------------------------------------------------


class TestAddMessage:
    def test_returns_message_dict(self, manager):
        msg = manager.add_message("peer", "user", "hello")
        assert msg["role"] == "user"
        assert msg["content"] == "hello"
        assert "timestamp" in msg

    def test_persists_to_disk(self, manager, home):
        manager.add_message("jarvis", "user", "Hello!")
        conv_file = home / "conversations" / "jarvis.json"
        assert conv_file.exists()
        data = json.loads(conv_file.read_text())
        assert data[0]["content"] == "Hello!"

    def test_atomic_write_no_tmp_file(self, manager, home):
        manager.add_message("ava", "user", "Test")
        tmp = home / "conversations" / "ava.json.tmp"
        assert not tmp.exists()

    def test_caps_at_max_history_messages(self, home):
        mgr = ConversationManager(home, max_history_messages=3)
        for i in range(5):
            mgr.add_message("peer", "user", f"msg {i}")
        history = mgr.get_history("peer")
        assert len(history) == 3
        assert history[-1]["content"] == "msg 4"

    def test_caps_persisted_file_too(self, home):
        mgr = ConversationManager(home, max_history_messages=3)
        for i in range(5):
            mgr.add_message("peer", "user", f"msg {i}")
        data = json.loads((home / "conversations" / "peer.json").read_text())
        assert len(data) == 3

    def test_multiple_peers_separate_files(self, manager, home):
        manager.add_message("alice", "user", "Hello from alice")
        manager.add_message("bob", "user", "Hello from bob")
        assert (home / "conversations" / "alice.json").exists()
        assert (home / "conversations" / "bob.json").exists()
        alice_data = json.loads((home / "conversations" / "alice.json").read_text())
        bob_data = json.loads((home / "conversations" / "bob.json").read_text())
        assert alice_data[0]["content"] == "Hello from alice"
        assert bob_data[0]["content"] == "Hello from bob"


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_finds_matching_content(self, manager):
        manager.add_message("alice", "user", "Let's debug this function")
        results = manager.search("debug")
        assert len(results) == 1
        assert results[0]["peer"] == "alice"
        assert results[0]["content"] == "Let's debug this function"

    def test_case_insensitive(self, manager):
        manager.add_message("peer", "user", "Hello World")
        assert len(manager.search("hello")) == 1
        assert len(manager.search("HELLO")) == 1
        assert len(manager.search("Hello")) == 1

    def test_no_match_returns_empty(self, manager):
        manager.add_message("peer", "user", "Something else")
        assert manager.search("quantum") == []

    def test_search_across_multiple_peers(self, manager):
        manager.add_message("alice", "user", "memory test one")
        manager.add_message("bob", "user", "memory test two")
        manager.add_message("charlie", "user", "unrelated message")
        results = manager.search("memory test")
        peers = {r["peer"] for r in results}
        assert "alice" in peers
        assert "bob" in peers
        assert "charlie" not in peers

    def test_result_includes_expected_fields(self, manager):
        manager.add_message("peer", "user", "test content")
        result = manager.search("test")[0]
        assert "peer" in result
        assert "role" in result
        assert "content" in result
        assert "timestamp" in result

    def test_empty_manager_returns_empty(self, manager):
        assert manager.search("anything") == []


# ---------------------------------------------------------------------------
# export_all
# ---------------------------------------------------------------------------


class TestExportAll:
    def test_empty_manager_returns_empty_dict(self, manager):
        assert manager.export_all() == {}

    def test_includes_all_peers(self, manager):
        manager.add_message("alice", "user", "hi")
        manager.add_message("bob", "user", "hey")
        exported = manager.export_all()
        assert set(exported.keys()) == {"alice", "bob"}

    def test_messages_preserved(self, manager):
        manager.add_message("alice", "user", "Hello")
        manager.add_message("alice", "assistant", "Hi!")
        exported = manager.export_all()
        assert len(exported["alice"]) == 2
        assert exported["alice"][0]["content"] == "Hello"

    def test_returns_copy_not_reference(self, manager):
        manager.add_message("peer", "user", "msg")
        exported = manager.export_all()
        exported["peer"].append({"role": "fake"})
        assert len(manager.export_all()["peer"]) == 1

    def test_loaded_from_disk_is_exported(self, populated_home):
        mgr = ConversationManager(populated_home)
        exported = mgr.export_all()
        assert "alice" in exported
        assert "bob" in exported


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_removes_from_memory(self, manager):
        manager.add_message("alice", "user", "hello")
        manager.delete("alice")
        assert manager.get_history("alice") == []

    def test_delete_removes_file(self, manager, home):
        manager.add_message("alice", "user", "hello")
        assert (home / "conversations" / "alice.json").exists()
        manager.delete("alice")
        assert not (home / "conversations" / "alice.json").exists()

    def test_delete_nonexistent_returns_false(self, manager):
        assert manager.delete("nobody") is False

    def test_delete_does_not_affect_other_peers(self, manager, home):
        manager.add_message("alice", "user", "hello")
        manager.add_message("bob", "user", "hey")
        manager.delete("bob")
        assert manager.get_history("alice") != []
        assert (home / "conversations" / "alice.json").exists()


# ---------------------------------------------------------------------------
# format_history_for_prompt
# ---------------------------------------------------------------------------


class TestFormatHistoryForPrompt:
    def test_empty_returns_empty_string(self, manager):
        assert manager.format_history_for_prompt("nobody") == ""

    def test_includes_peer_name_header(self, manager):
        manager.add_message("jarvis", "user", "Hello")
        result = manager.format_history_for_prompt("jarvis")
        assert "jarvis" in result

    def test_includes_message_content(self, manager):
        manager.add_message("jarvis", "user", "specific content here")
        result = manager.format_history_for_prompt("jarvis")
        assert "specific content here" in result

    def test_respects_max_messages(self, manager):
        for i in range(10):
            manager.add_message("peer", "user", f"message {i}")
        result = manager.format_history_for_prompt("peer", max_messages=3)
        # Only last 3 messages should appear
        assert "message 9" in result
        assert "message 7" in result
        assert "message 0" not in result


# ---------------------------------------------------------------------------
# Persistence round-trip
# ---------------------------------------------------------------------------


class TestPersistenceRoundTrip:
    def test_new_manager_picks_up_written_conversations(self, home):
        """Messages written by one ConversationManager are readable by another."""
        mgr1 = ConversationManager(home)
        mgr1.add_message("jarvis", "user", "Persistent hello")
        mgr1.add_message("jarvis", "assistant", "Persisted response")

        mgr2 = ConversationManager(home)
        history = mgr2.get_history("jarvis")
        assert len(history) == 2
        assert history[0]["content"] == "Persistent hello"
        assert history[1]["content"] == "Persisted response"

    def test_cap_honoured_on_reload(self, home):
        """History cap is applied when loading existing files."""
        conv_dir = home / "conversations"
        conv_dir.mkdir(parents=True, exist_ok=True)
        big_history = [
            {"role": "user", "content": f"msg {i}", "timestamp": "2026-01-01T00:00:00+00:00"}
            for i in range(20)
        ]
        (conv_dir / "peer.json").write_text(json.dumps(big_history), encoding="utf-8")

        mgr = ConversationManager(home, max_history_messages=5)
        history = mgr.get_history("peer")
        assert len(history) == 5
        assert history[-1]["content"] == "msg 19"
