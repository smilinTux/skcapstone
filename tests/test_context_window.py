"""Tests for skcapstone.context_window — per-sender context-window management.

Covers:
  * token estimators (tiktoken / chars//4 fallback)
  * under-budget history is left untouched
  * over-budget history is compressed: oldest messages summarized into a
    single sentinel entry while the most recent turns are preserved verbatim,
    and the resulting token count fits the budget.
  * ConversationStore.replace round-trips
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skcapstone.context_window import (
    ContextWindowManager,
    count_history_tokens,
    count_tokens,
)
from skcapstone.conversation_store import ConversationStore


class _FakeBridge:
    """Minimal LLMBridge stand-in returning a fixed one-paragraph summary."""

    def __init__(self, summary: str = "Prior chatter about deployments and tokens.") -> None:
        self.summary = summary
        self.calls = 0

    def generate(self, system_prompt: str, user_prompt: str, signal) -> str:  # noqa: D401
        self.calls += 1
        return self.summary


# ---------------------------------------------------------------------------
# Token estimators
# ---------------------------------------------------------------------------


def test_count_tokens_nonempty_positive():
    assert count_tokens("hello world") >= 1


def test_count_tokens_empty_is_one():
    # max(1, ...) floor guarantees non-zero
    assert count_tokens("") == 1


def test_count_history_tokens_sums_messages():
    history = [
        {"role": "user", "content": "a" * 40},
        {"role": "assistant", "content": "b" * 40},
    ]
    total = count_history_tokens(history)
    # roughly (40//4)*2 under the char fallback, but tiktoken may differ; just
    # assert it is a positive sum larger than a single message.
    assert total > count_tokens("a" * 40)


# ---------------------------------------------------------------------------
# ConversationStore.replace
# ---------------------------------------------------------------------------


def test_store_replace_roundtrip(tmp_path: Path):
    store = ConversationStore(tmp_path)
    store.append("bob", "user", "hi")
    store.replace("bob", [{"role": "system", "content": "compressed"}])
    loaded = store.load("bob")
    assert loaded == [{"role": "system", "content": "compressed"}]


# ---------------------------------------------------------------------------
# Under-budget: untouched
# ---------------------------------------------------------------------------


def test_under_budget_history_untouched(tmp_path: Path):
    store = ConversationStore(tmp_path)
    for i in range(6):
        store.append("alice", "user", f"short message {i}")
    before = store.load("alice")

    # Large budget → threshold never reached.
    mgr = ContextWindowManager(tmp_path, max_context_tokens=100_000)
    bridge = _FakeBridge()

    compressed = mgr.check_and_compress("alice", store, bridge)

    assert compressed is False
    assert bridge.calls == 0
    assert store.load("alice") == before  # verbatim, nothing rewritten
    stats = mgr.get_all_stats(store)["alice"]
    assert stats["messages"] == 6
    assert stats["pct_used"] < 80


# ---------------------------------------------------------------------------
# Over-budget: compressed, recent turns preserved
# ---------------------------------------------------------------------------


def test_over_budget_history_compressed_preserving_recent(tmp_path: Path):
    store = ConversationStore(tmp_path)
    # 20 fat messages so cumulative tokens blow past 80% of a tiny budget.
    big = "word " * 200  # ~1000 chars ≈ 250 tokens each (fallback)
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        store.append("carol", role, f"[{i}] {big}")

    before = store.load("carol")
    before_tokens = count_history_tokens(before)

    mgr = ContextWindowManager(tmp_path, max_context_tokens=2000)
    assert before_tokens >= int(2000 * 0.80)  # precondition: over threshold

    bridge = _FakeBridge(summary="Summary of the earlier 16 messages.")
    compressed = mgr.check_and_compress("carol", store, bridge)

    assert compressed is True
    assert bridge.calls == 1

    after = store.load("carol")
    # 1 summary sentinel + the 4 most recent verbatim messages
    assert len(after) == 5
    assert after[0].get("is_summary") is True
    assert "Summary of the earlier 16 messages." in after[0]["content"]

    # The 4 most recent original messages are preserved verbatim, in order.
    assert [m["content"] for m in after[1:]] == [m["content"] for m in before[-4:]]

    # Token count dropped and now fits under the full budget.
    after_tokens = count_history_tokens(after)
    assert after_tokens < before_tokens
    assert after_tokens < 2000

    stats = mgr.get_all_stats(store)["carol"]
    assert stats["last_compressed_at"] is not None


def test_over_budget_but_no_bridge_skips_compression(tmp_path: Path):
    store = ConversationStore(tmp_path)
    big = "word " * 200
    for i in range(20):
        store.append("dave", "user", f"[{i}] {big}")
    before = store.load("dave")

    mgr = ContextWindowManager(tmp_path, max_context_tokens=2000)
    # No bridge → stats updated but history left intact (fail-safe).
    compressed = mgr.check_and_compress("dave", store, bridge=None)

    assert compressed is False
    assert store.load("dave") == before
    stats = mgr.get_all_stats(store)["dave"]
    assert stats["pct_used"] >= 80
