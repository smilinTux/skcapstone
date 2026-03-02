"""Tests for ResponseCache — TTL, cache hit/miss, skip_cache wiring."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from skcapstone.blueprints.schema import ModelTier
from skcapstone.response_cache import ResponseCache, _TTL_CODE, _TTL_FAST, _ttl_for_tier, hash_prompt


# ---------------------------------------------------------------------------
# hash_prompt
# ---------------------------------------------------------------------------


class TestHashPrompt:
    """hash_prompt produces stable, distinct digests."""

    def test_deterministic(self):
        """Same inputs always produce the same hash."""
        h1 = hash_prompt("system", "hello")
        h2 = hash_prompt("system", "hello")
        assert h1 == h2

    def test_distinct_messages(self):
        """Different user messages produce different hashes."""
        h1 = hash_prompt("system", "hello")
        h2 = hash_prompt("system", "goodbye")
        assert h1 != h2

    def test_distinct_systems(self):
        """Different system prompts produce different hashes even with same user message."""
        h1 = hash_prompt("system-A", "hello")
        h2 = hash_prompt("system-B", "hello")
        assert h1 != h2

    def test_returns_hex_string(self):
        """Result is a 64-char lowercase hex string (SHA-256)."""
        h = hash_prompt("s", "u")
        assert len(h) == 64
        assert h == h.lower()
        int(h, 16)  # must parse as hex


# ---------------------------------------------------------------------------
# TTL helper
# ---------------------------------------------------------------------------


class TestTtlForTier:
    """_ttl_for_tier maps tiers to correct TTLs."""

    def test_fast_tier_ttl(self):
        assert _ttl_for_tier(ModelTier.FAST) == _TTL_FAST

    def test_code_tier_ttl(self):
        assert _ttl_for_tier(ModelTier.CODE) == _TTL_CODE

    def test_other_tiers_default_to_fast(self):
        """REASON, NUANCE, LOCAL, CUSTOM all get the FAST TTL (1 h)."""
        for tier in (ModelTier.REASON, ModelTier.NUANCE, ModelTier.LOCAL, ModelTier.CUSTOM):
            assert _ttl_for_tier(tier) == _TTL_FAST


# ---------------------------------------------------------------------------
# ResponseCache — basic put/get
# ---------------------------------------------------------------------------


class TestResponseCacheBasic:
    """Basic put/get, miss, empty-response guard."""

    def test_miss_on_empty_cache(self):
        """get() returns None for a key that was never stored."""
        cache = ResponseCache()
        assert cache.get("deadbeef" * 8, "gpt-4") is None

    def test_hit_after_put(self):
        """A stored entry is returned on subsequent get()."""
        cache = ResponseCache()
        ph = hash_prompt("sys", "hello")
        cache.put(ph, "llama3.2", ModelTier.FAST, "Hi there!")
        assert cache.get(ph, "llama3.2") == "Hi there!"

    def test_miss_different_model(self):
        """Cache is keyed by model name; wrong model → miss."""
        cache = ResponseCache()
        ph = hash_prompt("sys", "hello")
        cache.put(ph, "llama3.2", ModelTier.FAST, "Hi there!")
        assert cache.get(ph, "gpt-4") is None

    def test_miss_different_prompt(self):
        """Cache is keyed by prompt hash; different prompt → miss."""
        cache = ResponseCache()
        ph1 = hash_prompt("sys", "hello")
        ph2 = hash_prompt("sys", "goodbye")
        cache.put(ph1, "llama3.2", ModelTier.FAST, "Hi there!")
        assert cache.get(ph2, "llama3.2") is None

    def test_empty_response_not_stored(self):
        """put() silently ignores empty responses."""
        cache = ResponseCache()
        ph = hash_prompt("sys", "q")
        cache.put(ph, "llama3.2", ModelTier.FAST, "")
        assert cache.get(ph, "llama3.2") is None

    def test_size_tracks_entries(self):
        cache = ResponseCache()
        assert cache.size == 0
        ph = hash_prompt("sys", "x")
        cache.put(ph, "m1", ModelTier.FAST, "resp")
        assert cache.size == 1


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


class TestResponseCacheTTL:
    """Entries expire after their TTL elapses."""

    def test_entry_expires(self, monkeypatch):
        """An entry is evicted once monotonic time exceeds its expiry."""
        cache = ResponseCache()
        ph = hash_prompt("s", "u")

        # Freeze time at T=0 for put
        fake_time = [0.0]
        monkeypatch.setattr("skcapstone.response_cache.time.monotonic", lambda: fake_time[0])

        cache.put(ph, "llama3.2", ModelTier.FAST, "answer")
        assert cache.get(ph, "llama3.2") == "answer"  # alive at T=0

        # Advance past TTL
        fake_time[0] = _TTL_FAST + 1.0
        result = cache.get(ph, "llama3.2")
        assert result is None  # expired

    def test_code_tier_longer_ttl(self, monkeypatch):
        """CODE tier entries survive for 24 h but expire after."""
        cache = ResponseCache()
        ph = hash_prompt("s", "u")

        fake_time = [0.0]
        monkeypatch.setattr("skcapstone.response_cache.time.monotonic", lambda: fake_time[0])

        cache.put(ph, "claude-code", ModelTier.CODE, "code answer")

        # Still alive after 23 h
        fake_time[0] = 3600 * 23
        assert cache.get(ph, "claude-code") == "code answer"

        # Expired after 24 h + 1 s
        fake_time[0] = _TTL_CODE + 1.0
        assert cache.get(ph, "claude-code") is None


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


class TestResponseCacheEviction:
    """evict() removes expired entries; max_size cap is enforced."""

    def test_evict_removes_expired(self, monkeypatch):
        cache = ResponseCache()
        fake_time = [0.0]
        monkeypatch.setattr("skcapstone.response_cache.time.monotonic", lambda: fake_time[0])

        ph = hash_prompt("s", "u")
        cache.put(ph, "m1", ModelTier.FAST, "r1")
        assert cache.size == 1

        fake_time[0] = _TTL_FAST + 1.0
        removed = cache.evict()
        assert removed == 1
        assert cache.size == 0

    def test_max_size_enforced(self):
        """When max_size is exceeded, oldest entries are dropped."""
        cache = ResponseCache(max_size=3)
        for i in range(5):
            ph = hash_prompt("s", str(i))
            cache.put(ph, "m", ModelTier.FAST, f"resp-{i}")
        assert cache.size <= 3

    def test_clear_empties_cache(self):
        cache = ResponseCache()
        for i in range(5):
            ph = hash_prompt("s", str(i))
            cache.put(ph, "m", ModelTier.FAST, "resp")
        cache.clear()
        assert cache.size == 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestResponseCacheStats:
    """hits and misses are tracked correctly."""

    def test_hit_increments_hits(self):
        cache = ResponseCache()
        ph = hash_prompt("s", "u")
        cache.put(ph, "m", ModelTier.FAST, "r")
        cache.get(ph, "m")
        assert cache.stats["hits"] == 1
        assert cache.stats["misses"] == 0

    def test_miss_increments_misses(self):
        cache = ResponseCache()
        cache.get("no-such-hash", "model")
        assert cache.stats["misses"] == 1
        assert cache.stats["hits"] == 0


# ---------------------------------------------------------------------------
# LLMBridge integration — cache wired into generate()
# ---------------------------------------------------------------------------


class TestLLMBridgeCacheIntegration:
    """LLMBridge.generate() uses the cache and respects skip_cache."""

    def _make_bridge(self, cache):
        from skcapstone.consciousness_loop import ConsciousnessConfig, LLMBridge

        config = ConsciousnessConfig()
        with patch.object(LLMBridge, "_probe_available_backends"):
            bridge = LLMBridge(config, cache=cache)
        bridge._available = {"passthrough": True}
        return bridge

    def test_cache_hit_skips_llm(self):
        """When cache has a matching entry, the LLM callback is never called."""
        from skcapstone.model_router import TaskSignal

        cache = ResponseCache()
        bridge = self._make_bridge(cache)

        # Pre-populate the cache
        ph = hash_prompt("system", "hello")
        model = bridge._router.route(
            TaskSignal(description="hello", tags=["simple"])
        ).model_name
        cache.put(ph, model, ModelTier.FAST, "cached answer")

        with patch.object(bridge, "_resolve_callback") as mock_cb:
            result = bridge.generate("system", "hello", TaskSignal(description="hello", tags=["simple"]))

        mock_cb.assert_not_called()
        assert result == "cached answer"

    def test_skip_cache_bypasses_lookup_and_store(self):
        """When skip_cache=True the cache is never consulted or written."""
        from skcapstone.model_router import TaskSignal

        cache = ResponseCache()
        bridge = self._make_bridge(cache)

        fake_callback = MagicMock(return_value="live answer")
        with patch.object(bridge, "_resolve_callback", return_value=fake_callback):
            with patch.object(bridge, "_timed_call", return_value="live answer"):
                result = bridge.generate(
                    "system", "hello",
                    TaskSignal(description="hello", tags=["simple"]),
                    skip_cache=True,
                )

        assert result == "live answer"
        assert cache.size == 0  # nothing was stored

    def test_successful_call_populates_cache(self):
        """A successful LLM call populates the cache for next time."""
        from skcapstone.model_router import TaskSignal

        cache = ResponseCache()
        bridge = self._make_bridge(cache)

        with patch.object(bridge, "_timed_call", return_value="fresh answer"):
            with patch.object(bridge, "_resolve_callback", return_value=MagicMock()):
                bridge.generate(
                    "system", "hello",
                    TaskSignal(description="hello", tags=["simple"]),
                    skip_cache=False,
                )

        assert cache.size == 1

    def test_out_info_backend_is_cache_on_hit(self):
        """_out_info['backend'] is set to 'cache' on a cache hit."""
        from skcapstone.model_router import TaskSignal

        cache = ResponseCache()
        bridge = self._make_bridge(cache)

        signal = TaskSignal(description="hello", tags=["simple"])
        decision = bridge._router.route(signal)
        ph = hash_prompt("system", "hello")
        cache.put(ph, decision.model_name, decision.tier, "cached")

        out: dict = {}
        result = bridge.generate("system", "hello", signal, _out_info=out)
        assert result == "cached"
        assert out.get("backend") == "cache"
