"""Tests for the token-bucket rate limiter."""

from __future__ import annotations

import threading
import time

import pytest

from skcapstone.rate_limiter import RateLimiter, TokenBucket


# ---------------------------------------------------------------------------
# TokenBucket unit tests
# ---------------------------------------------------------------------------


class TestTokenBucket:
    def test_initial_full_bucket_allows_up_to_capacity(self):
        bucket = TokenBucket(rate=10.0, capacity=5)
        # Should allow exactly 5 consecutive requests from a full bucket
        for _ in range(5):
            assert bucket.consume() is True

    def test_exhausted_bucket_rejects(self):
        bucket = TokenBucket(rate=0.001, capacity=2)  # very slow refill
        bucket.consume()
        bucket.consume()
        assert bucket.consume() is False

    def test_tokens_refill_over_time(self):
        # Start with an empty-ish bucket (capacity=1, drain it, wait for refill)
        bucket = TokenBucket(rate=100.0, capacity=1)
        assert bucket.consume() is True   # drain
        assert bucket.consume() is False  # empty
        time.sleep(0.02)                  # wait 20 ms → ~2 tokens at 100/s
        assert bucket.consume() is True   # should be refilled

    def test_invalid_rate_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(rate=0, capacity=10)

    def test_invalid_capacity_raises(self):
        with pytest.raises(ValueError):
            TokenBucket(rate=1.0, capacity=0)


# ---------------------------------------------------------------------------
# RateLimiter unit tests
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_allows_requests_within_limit(self):
        limiter = RateLimiter(requests_per_minute=60)
        # First 60 requests from the same IP must all be allowed
        for _ in range(60):
            assert limiter.is_allowed("10.0.0.1") is True

    def test_blocks_after_limit_exceeded(self):
        limiter = RateLimiter(requests_per_minute=5)
        ip = "192.168.1.1"
        for _ in range(5):
            limiter.is_allowed(ip)
        assert limiter.is_allowed(ip) is False

    def test_different_ips_have_independent_buckets(self):
        limiter = RateLimiter(requests_per_minute=2)
        ip_a, ip_b = "1.1.1.1", "2.2.2.2"
        # Drain ip_a
        limiter.is_allowed(ip_a)
        limiter.is_allowed(ip_a)
        assert limiter.is_allowed(ip_a) is False
        # ip_b should still be untouched
        assert limiter.is_allowed(ip_b) is True

    def test_reset_restores_bucket(self):
        limiter = RateLimiter(requests_per_minute=1)
        ip = "10.0.0.5"
        limiter.is_allowed(ip)           # drain the single token
        assert limiter.is_allowed(ip) is False
        limiter.reset(ip)                # discard bucket
        assert limiter.is_allowed(ip) is True  # fresh bucket

    def test_clear_removes_all_buckets(self):
        limiter = RateLimiter(requests_per_minute=1)
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            limiter.is_allowed(ip)       # drain each
        limiter.clear()
        for ip in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
            assert limiter.is_allowed(ip) is True

    def test_invalid_rpm_raises(self):
        with pytest.raises(ValueError):
            RateLimiter(requests_per_minute=0)

    def test_requests_per_minute_property(self):
        limiter = RateLimiter(requests_per_minute=42)
        assert limiter.requests_per_minute == 42

    def test_concurrent_requests_thread_safe(self):
        """Multiple threads hammering the same IP should not crash or over-allow."""
        limiter = RateLimiter(requests_per_minute=50)
        ip = "10.0.0.99"
        allowed: list[bool] = []
        lock = threading.Lock()

        def hit():
            result = limiter.is_allowed(ip)
            with lock:
                allowed.append(result)

        threads = [threading.Thread(target=hit) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 50 should be allowed, rest rejected
        assert sum(allowed) == 50
        assert len(allowed) == 100
