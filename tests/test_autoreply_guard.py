"""Tests for the auto-reply loop guards — self-send guard + circuit breaker.

These cover the runaway agent<->agent reply storm that flooded the desktop
with thousands of notifications (opus and lumina daemons auto-replying to a
shared "lumina" mailbox with nothing to break the loop).
"""

from __future__ import annotations

from skcapstone.consciousness_loop import _AutoReplyGuard, _norm_identity


class TestNormIdentity:
    def test_strips_scheme_and_host(self):
        assert _norm_identity("capauth:lumina@skworld.io") == "lumina"

    def test_bare_handle_unchanged(self):
        assert _norm_identity("lumina") == "lumina"

    def test_case_insensitive(self):
        assert _norm_identity("Lumina") == "lumina"

    def test_host_only(self):
        assert _norm_identity("opus@skworld.io") == "opus"

    def test_none_safe(self):
        assert _norm_identity(None) == ""

    def test_scheme_and_bare_match(self):
        assert _norm_identity("capauth:lumina@skworld.io") == _norm_identity("lumina")


class TestAutoReplyGuard:
    def test_allows_up_to_threshold_then_trips(self):
        g = _AutoReplyGuard(max_replies=3, window_s=10.0, cooldown_s=100.0)
        assert g.allow("lumina", now=0.0) is True
        assert g.allow("lumina", now=1.0) is True
        assert g.allow("lumina", now=2.0) is True
        # 4th within window -> trips
        assert g.allow("lumina", now=3.0) is False

    def test_stays_tripped_during_cooldown(self):
        g = _AutoReplyGuard(max_replies=2, window_s=10.0, cooldown_s=100.0)
        g.allow("lumina", now=0.0)
        g.allow("lumina", now=1.0)
        assert g.allow("lumina", now=2.0) is False  # trips, cooldown until 102
        assert g.allow("lumina", now=50.0) is False  # still within cooldown

    def test_recovers_after_cooldown(self):
        g = _AutoReplyGuard(max_replies=2, window_s=10.0, cooldown_s=100.0)
        g.allow("lumina", now=0.0)
        g.allow("lumina", now=1.0)
        assert g.allow("lumina", now=2.0) is False  # trips at now=2 -> until 102
        assert g.allow("lumina", now=103.0) is True  # cooldown elapsed

    def test_peers_tracked_independently(self):
        g = _AutoReplyGuard(max_replies=2, window_s=10.0, cooldown_s=100.0)
        g.allow("lumina", now=0.0)
        g.allow("lumina", now=1.0)
        assert g.allow("lumina", now=2.0) is False  # lumina tripped
        assert g.allow("opus", now=2.0) is True      # opus unaffected

    def test_slow_traffic_never_trips(self):
        # max 3 per 10s window; one every 5s stays under the limit forever.
        g = _AutoReplyGuard(max_replies=3, window_s=10.0, cooldown_s=100.0)
        for i in range(20):
            assert g.allow("chef", now=i * 5.0) is True

    def test_normalized_peer_is_same_bucket(self):
        g = _AutoReplyGuard(max_replies=2, window_s=10.0, cooldown_s=100.0)
        g.allow("capauth:lumina@skworld.io", now=0.0)
        g.allow("lumina", now=1.0)
        # both forms count toward the same peer -> 3rd trips
        assert g.allow("lumina", now=2.0) is False
