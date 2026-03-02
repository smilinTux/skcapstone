"""Token-bucket rate limiter for the daemon HTTP API.

Each client IP gets an independent token bucket. Buckets refill
continuously at ``rate`` tokens/second up to ``capacity``.

Usage::

    limiter = RateLimiter(requests_per_minute=100)
    if not limiter.is_allowed("127.0.0.1"):
        # return HTTP 429
"""

from __future__ import annotations

import threading
import time
from typing import Dict


class TokenBucket:
    """Single-IP token bucket.

    Args:
        rate: Refill rate in tokens per second.
        capacity: Maximum token capacity (also the initial fill level).
    """

    def __init__(self, rate: float, capacity: int) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = float(capacity)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def consume(self, count: int = 1) -> bool:
        """Try to consume ``count`` tokens.

        Returns:
            True if the tokens were available and consumed, False otherwise.
        """
        with self._lock:
            self._refill()
            if self._tokens >= count:
                self._tokens -= count
                return True
            return False

    @property
    def tokens(self) -> float:
        """Current token level (approximate — does not acquire the lock)."""
        return self._tokens

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now


class RateLimiter:
    """Per-IP token-bucket rate limiter.

    Args:
        requests_per_minute: Allowed requests per minute per IP (default 100).
    """

    def __init__(self, requests_per_minute: int = 100) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self._rpm = requests_per_minute
        self._rate = requests_per_minute / 60.0  # tokens per second
        self._capacity = requests_per_minute
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def is_allowed(self, ip: str) -> bool:
        """Return True if the request from ``ip`` is within the rate limit."""
        bucket = self._get_or_create(ip)
        return bucket.consume()

    def reset(self, ip: str) -> None:
        """Remove the bucket for ``ip``, clearing its history."""
        with self._lock:
            self._buckets.pop(ip, None)

    def clear(self) -> None:
        """Remove all buckets (useful in tests)."""
        with self._lock:
            self._buckets.clear()

    @property
    def requests_per_minute(self) -> int:
        return self._rpm

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, ip: str) -> TokenBucket:
        with self._lock:
            if ip not in self._buckets:
                self._buckets[ip] = TokenBucket(self._rate, self._capacity)
            return self._buckets[ip]
