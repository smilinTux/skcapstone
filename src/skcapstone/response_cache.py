"""
Response Cache — TTL-based in-memory cache for LLM responses.

Caches responses keyed by (prompt_hash, model_name) with tier-dependent TTLs:
    - FAST tier: 1 hour
    - CODE tier: 24 hours
    - All other tiers: 1 hour (conservative default)

Conversation messages (real-time peer exchanges with dynamic context) must be
excluded at the call site by passing ``skip_cache=True`` to LLMBridge.generate().
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Optional

from skcapstone.blueprints.schema import ModelTier

logger = logging.getLogger("skcapstone.response_cache")

# TTL constants (seconds)
_TTL_FAST: float = 3600.0       # 1 hour
_TTL_CODE: float = 86400.0      # 24 hours
_TTL_DEFAULT: float = 3600.0    # 1 hour fallback for other tiers


def _ttl_for_tier(tier: ModelTier) -> float:
    """Return the cache TTL in seconds for a given model tier.

    Args:
        tier: The model tier used for the request.

    Returns:
        TTL in seconds: 3600 for FAST, 86400 for CODE, 3600 for all others.
    """
    if tier == ModelTier.CODE:
        return _TTL_CODE
    return _TTL_FAST  # FAST and all others default to 1 hour


def hash_prompt(system_prompt: str, user_message: str) -> str:
    """Compute a deterministic SHA-256 hex digest for a prompt pair.

    Args:
        system_prompt: The agent's system context string.
        user_message: The incoming user message.

    Returns:
        64-character lowercase hex string.
    """
    payload = f"{system_prompt}\x00{user_message}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _CacheEntry:
    """Internal container for a cached response and its expiry timestamp."""

    __slots__ = ("response", "expires_at")

    def __init__(self, response: str, ttl: float) -> None:
        self.response: str = response
        self.expires_at: float = time.monotonic() + ttl

    def is_alive(self) -> bool:
        """True if this entry has not yet expired."""
        return time.monotonic() < self.expires_at


class ResponseCache:
    """Thread-safe in-memory LLM response cache with per-tier TTLs.

    Entries are keyed by ``(prompt_hash, model_name)`` and expire automatically
    based on the tier that produced them.  A background sweep is *not* run;
    expired entries are evicted lazily on read and during periodic ``evict()``
    calls.

    Args:
        max_size: Maximum number of entries to keep.  Oldest entries are
            dropped when the limit is reached.  Defaults to 1024.
    """

    def __init__(self, max_size: int = 1024) -> None:
        self._max_size = max_size
        self._store: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(self, prompt_hash: str, model: str) -> Optional[str]:
        """Retrieve a cached response, returning None on miss or expiry.

        Args:
            prompt_hash: SHA-256 hex digest from :func:`hash_prompt`.
            model: Concrete model name (e.g. ``"llama3.2"``).

        Returns:
            Cached response string, or ``None`` if not found / expired.
        """
        key = (prompt_hash, model)
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None
            if not entry.is_alive():
                del self._store[key]
                self._misses += 1
                logger.debug("Cache miss (expired): model=%s", model)
                return None
            self._hits += 1
            logger.debug("Cache hit: model=%s", model)
            return entry.response

    def put(self, prompt_hash: str, model: str, tier: ModelTier, response: str) -> None:
        """Store a response in the cache.

        Args:
            prompt_hash: SHA-256 hex digest from :func:`hash_prompt`.
            model: Concrete model name.
            tier: Routing tier — determines the TTL.
            response: LLM response text to cache.
        """
        if not response:
            return
        ttl = _ttl_for_tier(tier)
        key = (prompt_hash, model)
        with self._lock:
            self._store[key] = _CacheEntry(response, ttl)
            if len(self._store) > self._max_size:
                self._evict_locked()
        logger.debug(
            "Cached response: model=%s tier=%s ttl=%.0fs len=%d",
            model, tier.value, ttl, len(response),
        )

    def evict(self) -> int:
        """Remove all expired entries and return the count removed.

        Returns:
            Number of entries evicted.
        """
        with self._lock:
            return self._evict_locked()

    def clear(self) -> None:
        """Remove all entries from the cache."""
        with self._lock:
            self._store.clear()

    @property
    def size(self) -> int:
        """Current number of entries (including not-yet-evicted expired ones)."""
        with self._lock:
            return len(self._store)

    @property
    def stats(self) -> dict[str, int]:
        """Return cache statistics: hits, misses, current size."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._store),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _evict_locked(self) -> int:
        """Evict expired entries.  Caller must hold ``self._lock``."""
        dead = [k for k, v in self._store.items() if not v.is_alive()]
        for k in dead:
            del self._store[k]
        if dead:
            logger.debug("Evicted %d expired cache entries", len(dead))

        # If still over limit after eviction, drop oldest by insertion order
        overflow = len(self._store) - self._max_size
        if overflow > 0:
            keys_to_drop = list(self._store.keys())[:overflow]
            for k in keys_to_drop:
                del self._store[k]
            logger.debug("Dropped %d entries to enforce max_size=%d", overflow, self._max_size)

        return len(dead)
