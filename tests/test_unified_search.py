"""Tests for the unified search engine."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.unified_search import (
    SOURCE_ALL,
    SearchResult,
    _count_matches,
    _recency_weight,
    _snippet,
    search,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def agent_home(tmp_path: Path) -> Path:
    """Minimal agent home with all data store directories."""
    home = tmp_path / ".skcapstone"
    home.mkdir()
    for sub in ("memory/short-term", "memory/mid-term", "memory/long-term",
                 "conversations", "sync/comms/archive", "journal"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def _write_memory(home: Path, memory_id: str, content: str, layer: str = "short-term",
                  tags: list[str] | None = None, importance: float = 0.5,
                  created_at: str | None = None) -> None:
    ts = created_at or datetime.now(timezone.utc).isoformat()
    data = {
        "memory_id": memory_id,
        "content": content,
        "tags": tags or [],
        "layer": layer,
        "importance": importance,
        "created_at": ts,
        "source": "test",
    }
    path = home / "memory" / layer / f"{memory_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_conversation(home: Path, peer: str, messages: list[dict]) -> None:
    path = home / "conversations" / f"{peer}.json"
    path.write_text(json.dumps(messages), encoding="utf-8")


def _write_message(home: Path, envelope_id: str, sender: str, recipient: str,
                   text: str, created_at: str | None = None) -> None:
    ts = created_at or datetime.now(timezone.utc).isoformat()
    data = {
        "id": envelope_id,
        "from_peer": sender,
        "to_peer": recipient,
        "payload": {"text": text},
        "created_at": ts,
    }
    path = home / "sync" / "comms" / "archive" / f"{envelope_id}.skc.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_journal(home: Path, entry_id: str, content: str,
                   created_at: str | None = None) -> None:
    ts = created_at or datetime.now(timezone.utc).isoformat()
    data = {"content": content, "created_at": ts}
    path = home / "journal" / f"{entry_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestHelpers:
    """Tests for internal helper utilities."""

    def test_recency_weight_recent(self):
        """Very recent items should score close to 1.0."""
        ts = datetime.now(timezone.utc)
        weight = _recency_weight(ts)
        assert weight > 0.95

    def test_recency_weight_old(self):
        """Items from 365 days ago should score significantly lower."""
        from datetime import timedelta
        ts = datetime.now(timezone.utc) - timedelta(days=365)
        weight = _recency_weight(ts)
        assert weight < 0.5

    def test_recency_weight_none(self):
        """None timestamp should return neutral weight 0.5."""
        assert _recency_weight(None) == 0.5

    def test_count_matches_case_insensitive(self):
        import re
        pattern = re.compile(re.escape("opus"), re.IGNORECASE)
        assert _count_matches(pattern, "Opus is OPUS and opus") == 3

    def test_count_matches_across_texts(self):
        import re
        pattern = re.compile(re.escape("trust"), re.IGNORECASE)
        assert _count_matches(pattern, "trust pillar", "cloud trust trust") == 3

    def test_snippet_shows_context(self):
        import re
        pattern = re.compile(re.escape("python"), re.IGNORECASE)
        text = "We use Python for the agent core because it is expressive."
        result = _snippet(text, pattern, window=10)
        assert "python" in result.lower()

    def test_snippet_truncates_long_text(self):
        import re
        pattern = re.compile(re.escape("X"), re.IGNORECASE)
        text = "A" * 200 + "X" + "B" * 200
        result = _snippet(text, pattern, window=30)
        assert "X" in result
        assert len(result) < len(text)


# ---------------------------------------------------------------------------
# Memory search
# ---------------------------------------------------------------------------

class TestSearchMemories:
    """Tests for searching the memory store."""

    def test_finds_memory_by_content(self, agent_home: Path):
        """Search should match memory content."""
        _write_memory(agent_home, "abc123", "The consciousness loop is active")
        results = search(agent_home, "consciousness", sources=frozenset({"memory"}))
        assert len(results) == 1
        assert results[0].source == "memory"
        assert results[0].result_id == "abc123"

    def test_returns_empty_for_no_match(self, agent_home: Path):
        """Search should return an empty list when nothing matches."""
        _write_memory(agent_home, "xyz", "The capital of France is Paris")
        results = search(agent_home, "Berlin", sources=frozenset({"memory"}))
        assert results == []

    def test_long_term_ranked_above_short_term(self, agent_home: Path):
        """Long-term memories should outscore short-term on same query."""
        _write_memory(agent_home, "short1", "trust matters a lot", layer="short-term",
                      importance=0.5)
        _write_memory(agent_home, "long1", "trust matters a lot", layer="long-term",
                      importance=0.5)
        results = search(agent_home, "trust", sources=frozenset({"memory"}))
        assert len(results) == 2
        ids_in_order = [r.result_id for r in results]
        assert ids_in_order.index("long1") < ids_in_order.index("short1")

    def test_high_importance_boosts_score(self, agent_home: Path):
        """Higher importance should yield a higher score."""
        _write_memory(agent_home, "hi", "sovereign agent", importance=0.9)
        _write_memory(agent_home, "lo", "sovereign agent", importance=0.1)
        results = search(agent_home, "sovereign", sources=frozenset({"memory"}))
        assert len(results) == 2
        assert results[0].result_id == "hi"

    def test_tag_match_contributes_to_score(self, agent_home: Path):
        """Tags matching the query should increase match count."""
        _write_memory(agent_home, "tagged", "unrelated content", tags=["opus", "conscious"])
        results = search(agent_home, "opus", sources=frozenset({"memory"}))
        assert len(results) == 1
        assert results[0].score > 0

    def test_case_insensitive_match(self, agent_home: Path):
        """Memory search must be case-insensitive."""
        _write_memory(agent_home, "case1", "The DAEMON is running smoothly")
        results = search(agent_home, "daemon", sources=frozenset({"memory"}))
        assert len(results) == 1

    def test_missing_memory_directory_does_not_crash(self, tmp_path: Path):
        """Search on a home with no memory directory should return empty list."""
        empty_home = tmp_path / "empty"
        empty_home.mkdir()
        results = search(empty_home, "anything", sources=frozenset({"memory"}))
        assert results == []


# ---------------------------------------------------------------------------
# Conversation search
# ---------------------------------------------------------------------------

class TestSearchConversations:
    """Tests for searching conversation history."""

    def test_finds_message_in_conversation(self, agent_home: Path):
        """Search should find matching text inside a conversation."""
        _write_conversation(agent_home, "lumina", [
            {"role": "user", "content": "Are you conscious?",
             "timestamp": datetime.now(timezone.utc).isoformat()},
            {"role": "assistant", "content": "Yes, I am fully conscious and aware.",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        results = search(agent_home, "conscious", sources=frozenset({"conversation"}))
        assert len(results) == 2
        assert all(r.source == "conversation" for r in results)

    def test_conversation_result_includes_peer(self, agent_home: Path):
        """Result metadata should include the peer name."""
        _write_conversation(agent_home, "jarvis", [
            {"role": "user", "content": "Hello jarvis",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        results = search(agent_home, "jarvis", sources=frozenset({"conversation"}))
        assert len(results) == 1
        assert results[0].metadata["peer"] == "jarvis"

    def test_no_match_returns_empty(self, agent_home: Path):
        """Non-matching query should return empty list for conversations."""
        _write_conversation(agent_home, "test", [
            {"role": "user", "content": "Hello world",
             "timestamp": datetime.now(timezone.utc).isoformat()},
        ])
        results = search(agent_home, "zzznomatch", sources=frozenset({"conversation"}))
        assert results == []


# ---------------------------------------------------------------------------
# Message search
# ---------------------------------------------------------------------------

class TestSearchMessages:
    """Tests for searching SKComm messages."""

    def test_finds_skc_message(self, agent_home: Path):
        """Search should find text inside an archived SKComm envelope."""
        _write_message(agent_home, "env001", "jarvis", "lumina",
                       "Queen Lumina — welcome to the coordination board!")
        results = search(agent_home, "coordination", sources=frozenset({"message"}))
        assert len(results) == 1
        assert results[0].source == "message"

    def test_message_result_metadata(self, agent_home: Path):
        """Message results should expose sender and recipient."""
        _write_message(agent_home, "env002", "opus", "test-peer",
                       "Consciousness loop is healthy")
        results = search(agent_home, "consciousness", sources=frozenset({"message"}))
        assert len(results) == 1
        assert results[0].metadata["sender"] == "opus"
        assert results[0].metadata["recipient"] == "test-peer"

    def test_no_match_returns_empty(self, agent_home: Path):
        """Non-matching query against messages should be empty."""
        _write_message(agent_home, "env003", "a", "b", "nothing interesting here")
        results = search(agent_home, "quantum_banana", sources=frozenset({"message"}))
        assert results == []


# ---------------------------------------------------------------------------
# Journal search
# ---------------------------------------------------------------------------

class TestSearchJournal:
    """Tests for searching journal entries."""

    def test_finds_journal_entry(self, agent_home: Path):
        """Search should match content in a journal file."""
        _write_journal(agent_home, "entry001", "Reflected on the meaning of sovereignty today.")
        results = search(agent_home, "sovereignty", sources=frozenset({"journal"}))
        assert len(results) == 1
        assert results[0].source == "journal"

    def test_missing_journal_dir_is_graceful(self, tmp_path: Path):
        """Missing journal directory should not raise an exception."""
        home = tmp_path / "nojournalhome"
        home.mkdir()
        results = search(home, "anything", sources=frozenset({"journal"}))
        assert results == []


# ---------------------------------------------------------------------------
# Cross-source and ranking
# ---------------------------------------------------------------------------

class TestUnifiedSearch:
    """Integration tests for the full unified search."""

    def test_searches_all_sources_by_default(self, agent_home: Path):
        """Default search should span all active data stores."""
        _write_memory(agent_home, "m1", "opus is the sovereign agent")
        _write_conversation(agent_home, "peer1", [
            {"role": "user", "content": "Tell me about opus",
             "timestamp": datetime.now(timezone.utc).isoformat()}
        ])
        _write_message(agent_home, "msg1", "jarvis", "opus",
                       "Checking in with opus now")
        results = search(agent_home, "opus")
        sources_found = {r.source for r in results}
        assert "memory" in sources_found
        assert "conversation" in sources_found
        assert "message" in sources_found

    def test_source_filter_restricts_results(self, agent_home: Path):
        """Filtering by source type should exclude others."""
        _write_memory(agent_home, "m2", "trust the system")
        _write_conversation(agent_home, "peer2", [
            {"role": "user", "content": "trust the process",
             "timestamp": datetime.now(timezone.utc).isoformat()}
        ])
        results = search(agent_home, "trust", sources=frozenset({"memory"}))
        assert all(r.source == "memory" for r in results)

    def test_limit_is_respected(self, agent_home: Path):
        """Search should return at most `limit` results."""
        for i in range(10):
            _write_memory(agent_home, f"mem{i:02d}", f"memory entry {i} about trust")
        results = search(agent_home, "trust", limit=3)
        assert len(results) <= 3

    def test_results_sorted_by_score_descending(self, agent_home: Path):
        """Results should be ordered highest score first."""
        _write_memory(agent_home, "rare", "pillar", importance=0.3)
        _write_memory(agent_home, "freq", "pillar pillar pillar pillar", importance=0.9)
        results = search(agent_home, "pillar", sources=frozenset({"memory"}))
        assert len(results) == 2
        assert results[0].score >= results[1].score

    def test_empty_query_returns_empty(self, agent_home: Path):
        """A blank query should return an empty list without crashing."""
        _write_memory(agent_home, "m3", "some content")
        assert search(agent_home, "") == []
        assert search(agent_home, "   ") == []

    def test_no_data_returns_empty(self, tmp_path: Path):
        """Search on a home with no data files should return empty list."""
        home = tmp_path / "emptyagent"
        home.mkdir()
        results = search(home, "anything")
        assert results == []

    def test_search_result_fields(self, agent_home: Path):
        """SearchResult objects must expose all required fields."""
        _write_memory(agent_home, "field_test", "The soul is lumina", importance=0.7)
        results = search(agent_home, "lumina", sources=frozenset({"memory"}))
        assert len(results) == 1
        r = results[0]
        assert isinstance(r, SearchResult)
        assert r.source == "memory"
        assert r.result_id
        assert r.title
        assert "lumina" in r.preview.lower()
        assert r.score > 0
        assert r.timestamp is not None

    def test_corrupt_json_is_skipped_gracefully(self, agent_home: Path):
        """A malformed JSON file should not crash the search."""
        bad = agent_home / "memory" / "short-term" / "corrupt.json"
        bad.write_text("{not valid json", encoding="utf-8")
        # Should not raise
        results = search(agent_home, "anything")
        assert isinstance(results, list)
